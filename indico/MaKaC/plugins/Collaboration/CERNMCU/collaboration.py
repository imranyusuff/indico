# -*- coding: utf-8 -*-
##
##
## This file is part of CDS Indico.
## Copyright (C) 2002, 2003, 2004, 2005, 2006, 2007 CERN.
##
## CDS Indico is free software; you can redistribute it and/or
## modify it under the terms of the GNU General Public License as
## published by the Free Software Foundation; either version 2 of the
## License, or (at your option) any later version.
##
## CDS Indico is distributed in the hope that it will be useful, but
## WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
## General Public License for more details.
##
## You should have received a copy of the GNU General Public License
## along with CDS Indico; if not, write to the Free Software Foundation, Inc.,
## 59 Temple Place, Suite 330, Boston, MA 02111-1307, USA.

from MaKaC.common.PickleJar import DictPickler
from MaKaC.common.timezoneUtils import nowutc, setAdjustedDate, getAdjustedDate
from MaKaC.plugins.Collaboration.base import CSBookingBase
from MaKaC.plugins.Collaboration.CERNMCU.common import CERNMCUException,\
    ParticipantPerson, ParticipantRoom, getCERNMCUOptionValueByName,\
    CERNMCUError, handleSocketError, getMinStartDate, getMaxEndDate
from MaKaC.common.utils import formatDateTime, validIP, unicodeLength,\
    unicodeSlice
from MaKaC.plugins.Collaboration.CERNMCU.mcu import MCU, MCUConfCommonParams, MCUTime,\
    paramsForLog, MCUParams, MCUParticipantCommonParams, datetimeFromMCUTime
from MaKaC.i18n import _

from xmlrpclib import Fault
from datetime import timedelta
from MaKaC.common.logger import Logger
from MaKaC.common.Counter import Counter
from MaKaC.services.interface.rpc.json import unicodeToUtf8
import socket

class CSBooking(CSBookingBase):

    _hasTitle = True
    _hasStart = True
    _hasStop = True
    _hasStartStopAll = True
    _hasCheckStatus = True

    _requiresServerCallForStart = True
    _requiresClientCallForStart = False

    _requiresServerCallForStop = True
    _requiresClientCallForStop = False

    _needsBookingParamsCheck = True
    _needsToBeNotifiedOnView = True

    _hasEventDisplay = True

    _commonIndexes = ["All Videoconference"]

    _simpleParameters = {
        "name": (str, ''),
        "description": (str, ''),
        "id": (str, ''),
        "displayPin": (bool, False)}

    _complexParameters = ["pin", "hasPin", "autoGenerateId", "customId", "participants"]

    def __init__(self, type, conf):
        CSBookingBase.__init__(self, type, conf)

        self._oldName = None
        self._pin = None
        self._autoGeneratedId = None #boolean storing if the id was generated by Indico (True) or chosen by user (False)
        self._customId = None #the custom id chosen by the user, if any
        self._participants = {} #{id, Participant object}
        self._participantIdCounter = Counter(1)

        self._created = False
        self._creationTriesCounter = 0
        self._hasBeenStarted = False

    def getMCUStartTime(self):
        return MCUTime(self.getAdjustedStartDate(getCERNMCUOptionValueByName("MCUTZ")))

    def getDurationSeconds(self):
        diff = self.getEndDate() - self.getStartDate()

        #we have to do this for conferences where the start and end dates are on both sides of a summer time change
        #because the MCU is not timezone aware (blame the guy who programmed it)
        mcuTimezone = getCERNMCUOptionValueByName("MCUTZ")
        timeChangeDifference = self.getAdjustedStartDate(mcuTimezone).utcoffset() - self.getAdjustedEndDate(mcuTimezone).utcoffset()
        diff = diff - timeChangeDifference

        return diff.days * 86400 + diff.seconds

    def setDurationSeconds(self, durationSeconds):
        #we have to do this for conferences where the start and end dates are on both sides of a summer time change
        #because the MCU is not timezone aware (blame the guy who programmed it)
        tempEndDate = self.getStartDate() + timedelta(seconds = durationSeconds)
        mcuTimezone = getCERNMCUOptionValueByName("MCUTZ")
        timeChangeDifference = self.getAdjustedStartDate(mcuTimezone).utcoffset() - getAdjustedDate(tempEndDate, tz = mcuTimezone).utcoffset()

        self.setEndDate(tempEndDate + timeChangeDifference)

    def setAutoGenerateId(self, autoGenerateId):
        self._autoGeneratedId = (autoGenerateId == 'yes')

    def getAutoGenerateId(self):
        if self._autoGeneratedId:
            return 'yes'
        else:
            return 'no'

    def setCustomId(self, customId):
        self._customId = customId

    def getCustomId(self):
        if self._autoGeneratedId:
            return ''
        else:
            return self._customId

    def getPin(self):
        """ This method returns the pin that will be displayed in the indico page
        """
        return self._pin

    def setPin(self, pin):
        if not pin or pin.strip() == "":
            self._pin = ""
        else:
            self._pin = pin

    def getHasPin(self):
        return self._pin is not None and len(self._pin) > 0

    def setHasPin(self, value):
        #ignore, will be called only on rollback
        pass

    def getParticipantList(self, sorted = False):
        if sorted:
            keys = self._participants.keys()
            keys.sort()
            return [self._participants[k] for k in keys]
        else:
            return self._participants.values()

    def getParticipants(self):
        return DictPickler.pickle(self.getParticipantList(sorted = True))

    def setParticipants(self, participants):
        participantsCopy = dict(self._participants)
        self._participants = {}
        for p in participants:
            id = p.get("participantId", None)
            if id is None or not id in participantsCopy:
                id = self._participantIdCounter.newCount()

            if p["type"] == 'person':
                participantObject = ParticipantPerson(self, id, p)
            elif p["type"] == "room":
                participantObject = ParticipantRoom(self, id, p)

            self._participants[id] = participantObject

        self._p_changed = 1


    ## overriding methods
    def _getTitle(self):
        return self._bookingParams["name"]


    def _checkBookingParams(self):
        if len(self._bookingParams["name"].strip()) == 0:
            raise CERNMCUException("name parameter (" + str(self._bookingParams["name"]) +") is empty for booking with id: " + str(self._id))

        if unicodeLength(self._bookingParams["name"]) >= 32:
            raise CERNMCUException("name parameter (" + str(self._bookingParams["name"]) +") is longer than 31 characters for booking with id: " + str(self._id))

        self._bookingParams["name"] = self._bookingParams["name"].strip()

        if len(self._bookingParams["description"].strip()) == 0:
            raise CERNMCUException("description parameter (" + str(self._bookingParams["description"]) +") is empty for booking with id: " + str(self._id))

        if not self._autoGeneratedId:
            if len(self._customId.strip()) == 0:
                raise CERNMCUException("customId parameter (" + str(self._customId) +") is empty for booking with id: " + str(self._id))
            else:
                try:
                    int(self._customId)
                except ValueError:
                    raise CERNMCUException("customId parameter (" + str(self._customId) +") is not an integer for booking with id: " + str(self._id))
                if len(self._customId.strip()) != 5:
                    raise CERNMCUException("customId parameter (" + str(self._customId) +") is longer than 5 digits for booking with id: " + str(self._id))
                self._customId = int(self._customId)

        if self._pin:
            try:
                int(self._pin)
            except ValueError:
                raise CERNMCUException("pin parameter (" + str(self._pin) +") is not an integer for booking with id: " + str(self._id))

            if len(self._pin) >= 32:
                raise CERNMCUException("pin parameter (" + str(self._pin) +") is longer than 31 characters for booking with id: " + str(self._id))

        #if self.getAdjustedStartDate('UTC')  < (nowutc()):
        #    raise CERNMCUException("Cannot create booking in the past. Booking id: %s"% (str(self._id)))

        if self.getAdjustedEndDate('UTC')  < (nowutc()):
            raise CERNMCUException("End date cannot be in the past. Booking id: %s"% (str(self._id)))

        minStartDate = getMinStartDate(self.getConference())
        if self.getAdjustedStartDate() < minStartDate:
            raise CERNMCUException("Cannot create a booking before the Indico event's start date. Please create it after %s"%(formatDateTime(minStartDate)))

        maxEndDate = getMaxEndDate(self.getConference())
        if self.getAdjustedStartDate() > maxEndDate:
            raise CERNMCUException("Cannot create a booking after before the Indico event's end date. Please create it after %s"%(formatDateTime(maxEndDate)))

        pSet = set()
        ipSet = set()
        for p in self._participants.itervalues():
            if not validIP(p.getIp()):
                raise CERNMCUException("Participant has not a correct ip. (ip string= " + p.getIp() + ")")

            if p.getType() == 'person':
                if not p.getFamilyName():
                    raise CERNMCUException(_("Participant (person) does not have family name."))
                if not p.getFirstName():
                    raise CERNMCUException(_("Participant (person) does not have first name."))
            elif p.getType() == 'room':
                if not p.getName():
                    raise CERNMCUException(_("Participant (room) does not have name."))

            name = p.getParticipantName()
            if name in pSet:
                raise CERNMCUException(_("At least two of the participants will have the same name in the MCU. Please change their name, affiliation, ip, etc."))
            else:
                pSet.add(name)

            ip = p.getIp()
            if ip in ipSet:
                raise CERNMCUException(_("At least two of the participants have the same IP. Please change this"))
            else:
                ipSet.add(ip)

        return False

    def _create(self):
        if self._autoGeneratedId:
            if self._creationTriesCounter < 100:
                id = self._plugin.getGlobalData().getNewConferenceId()
            else:
                return CERNMCUError('tooManyTries', "Could not obtain ID")
        else:
            id = self._customId

        try:
            mcu = MCU.getInstance()
            params = MCUConfCommonParams(conferenceName = self._bookingParams["name"],
                                         numericId = str(id),
                                         startTime = self.getMCUStartTime(),
                                         durationSeconds = self.getDurationSeconds(),
                                         pin = self._pin,
                                         description = unicodeSlice(self._bookingParams["description"], 0, 31),
                                        )
            Logger.get('CERNMCU').info("""Evt:%s, booking:%s, calling conference.create with params: %s""" % (self._conf.getId(), self.getId(), str(paramsForLog(params))))
            answer = unicodeToUtf8(mcu.conference.create(params))
            Logger.get('CERNMCU').info("""Evt:%s, booking:%s, calling conference.create. Got answer: %s""" % (self._conf.getId(), self.getId(), str(answer)))

            for p in self._participants.itervalues():
                result = self.addParticipant(p)
                if not result is True:
                    return result

            self._statusMessage = _("Booking created")
            self._statusClass = "statusMessageOK"
            self._bookingParams["id"] = id
            self._oldName = self._bookingParams["name"]
            self._created = True
            self.checkCanStart()

        except Fault, e:
            Logger.get('CERNMCU').warning("""Evt:%s, calling conference.create. Got error: %s""" % (self._conf.getId(), str(e)))
            return self.handleFault('create', e)

        except socket.error, e:
            handleSocketError(e)

    def _modify(self):
        """ Relays to the MCU the changes donde by the user to the Indico booking object.
            For the participants, we retrieve a list of existing participants.
            If a participant is both in the MCU and the Indico booking, it is not touched.
            Thus, we only delete in the MCU those having been removed, and we only create only those who have been added.
            In this way we avoid disconnection of already connected participants.
        """

        if self._created:

            if self._autoGeneratedId:
                id = self._bookingParams["id"]
            else:
                id = self._customId

            try:
                mcu = MCU.getInstance()
                params = MCUConfCommonParams(conferenceName = self._oldName,
                                         newConferenceName = self._bookingParams["name"],
                                         numericId = str(id),
                                         startTime = self.getMCUStartTime(),
                                         durationSeconds = self.getDurationSeconds(),
                                         pin = self._pin,
                                         description = self._bookingParams["description"],
                                         )
                Logger.get('CERNMCU').info("""Evt:%s, booking:%s, calling conference.modify with params: %s""" % (self._conf.getId(), self.getId(), str(paramsForLog(params))))
                answer = unicodeToUtf8(mcu.conference.modify(params))
                Logger.get('CERNMCU').info("""Evt:%s, booking:%s, calling conference.modify. Got answer: %s""" % (self._conf.getId(), self.getId(), str(answer)))
                self._bookingParams["id"] = id
                self._oldName = self._bookingParams["name"]


                #we take care of the participants
                remoteParticipants = self.queryParticipants()
                existingInBoth = {} #key: participantName, value: a Participant object

                for p in self._participants.itervalues():
                    name = p.getParticipantName()
                    if not name in remoteParticipants:
                        result = self.addParticipant(p)
                        if not result is True:
                            return result
                    else:
                        existingInBoth[name] = p

                for participantName, localParticipant in existingInBoth.iteritems():
                    remoteParticipant = remoteParticipants[participantName]
                    if remoteParticipant["ip"] != localParticipant.getIp():
                        self.removeParticipant(participantName)
                        self.addParticipant(localParticipant)
                    elif remoteParticipant["displayName"] != localParticipant.getDisplayName():
                        self.modifyParticipantDisplayName(participantName, localParticipant.getDisplayName())

                participantNamesToBeRemoved = set(remoteParticipants) - set(existingInBoth)
                for name in participantNamesToBeRemoved:
                    result = self.removeParticipant(name)
                    if not result is True:
                        return result

                self._created = True
                self.checkCanStart()


            except Fault, e:
                Logger.get('CERNMCU').warning("""Evt:%s, booking:%s, calling conference.modify. Got error: %s""" % (self._conf.getId(), self.getId(), str(e)))
                return self.handleFault('modify', e)

            except socket.error, e:
                handleSocketError(e)

        else: #not yet created because of errors: try to recreate
            self._create()

    def _start(self):
        self._checkStatus()
        if self._canBeStarted:
            try:
                mcu = MCU.getInstance()
                for p in self.getParticipantList(sorted = True):
                    params = MCUParams(conferenceName = self._bookingParams["name"],
                                       participantName = p.getParticipantName())
                    Logger.get('CERNMCU').info("""Evt:%s, booking:%s, calling participant.connect with params: %s""" % (self._conf.getId(), self.getId(), str(paramsForLog(params))))
                    answer = unicodeToUtf8(mcu.participant.connect(params))
                    Logger.get('CERNMCU').info("""Evt:%s, booking:%s, calling participant.connect. Got answer: %s""" % (self._conf.getId(), self.getId(), str(answer)))

                self._statusMessage = _("Conference started!")
                self._canBeStarted = False
                self._canBeStopped = True
                self._hasBeenStarted = True

            except Fault, e:
                Logger.get('CERNMCU').warning("""Evt:%s, booking:%s, calling participant.connect. Got error: %s""" % (self._conf.getId(), self.getId(), str(e)))
                return self.handleFault('start', e)

            except socket.error, e:
                handleSocketError(e)
        else:
            raise CERNMCUException(_("Conference cannot start yet!"))

    def _stop(self):
        self._checkStatus()
        if self._canBeStopped:
            try:
                mcu = MCU.getInstance()
                for p in self.getParticipantList(sorted = True):
                    try:
                        params = MCUParams(conferenceName = self._bookingParams["name"],
                                           participantName = p.getParticipantName())
                        Logger.get('CERNMCU').info("""Evt:%s, booking:%s, calling participant.disconnect with params: %s""" % (self._conf.getId(), self.getId(), str(paramsForLog(params))))
                        answer = unicodeToUtf8(mcu.participant.disconnect(params))
                        Logger.get('CERNMCU').info("""Evt:%s, booking:%s, calling participant.disconnect. Got answer: %s""" % (self._conf.getId(), self.getId(), str(answer)))
                    except Fault, e:
                        Logger.get('CERNMCU').warning("""Evt:%s, booking:%s, calling participant.disconnect. Got error: %s""" % (self._conf.getId(), self.getId(), str(e)))
                        fault = self.handleFault('stop', e)
                        if fault:
                            return fault

                self._statusMessage = _("Conference stopped")
                self._statusClass = "statusMessageOther"
                self._canBeStarted = True
                self._canBeStopped = False
                self._hasBeenStarted = False

            except Fault, e:
                Logger.get('CERNMCU').warning("""Evt:%s, booking:%s, calling participant.disconnect. Got error: %s""" % (self._conf.getId(), self.getId(), str(e)))
                return self.handleFault('stop', e)

            except socket.error, e:
                handleSocketError(e)
        else:
            raise CERNMCUException(_("Conference cannot be stopped"))

    def _notifyOnView(self):
        self.checkCanStart()

    def _checkStatus(self):
        if self._created:
            self.queryConference()
            self.checkCanStart()

    def _delete(self, oldName = None):
        if self._created:
            if oldName:
                name = oldName
            else:
                name = self._bookingParams["name"]

            try:
                mcu = MCU.getInstance()
                params = MCUParams(conferenceName = name)
                Logger.get('CERNMCU').info("""Evt:%s, booking:%s, calling conference.destroy with params: %s""" % (self._conf.getId(), self.getId(), str(paramsForLog(params))))
                answer = unicodeToUtf8(mcu.conference.destroy(params))
                Logger.get('CERNMCU').info("""Evt:%s, booking:%s, calling conference.destroy. Got answer: %s""" % (self._conf.getId(), self.getId(), str(answer)))

                if not oldName:
                    self._created = False
            except Fault, e:
                Logger.get('CERNMCU').warning("""Evt:%s, booking:%s, calling conference.destroy. Got error: %s""" % (self._conf.getId(), self.getId(), str(e)))
                if e.faultCode == 4: #conference didn't exist in the MCU, but we delete it from Indico anyway
                    pass
                else:
                    return self.handleFault('delete', e)
            except socket.error, e:
                handleSocketError(e)
        else:
            self._error = False

    ## end of overrided methods

    def addParticipant(self, participant):
        """ Adds a participant to the MCU conference represented by this booking.
            participant: a Participant object
            returns: True if successful, a CERNMCUError if there is a problem in some cases, raises an Exception in others
        """
        try:
            mcu = MCU.getInstance()

            params = MCUParticipantCommonParams(conferenceName = self._bookingParams["name"],
                                                participantName = participant.getParticipantName(),
                                                displayNameOverrideValue = participant.getDisplayName(),
                                                address = participant.getIp()
                                                )
            Logger.get('CERNMCU').info("""Evt:%s, booking:%s, calling participant.add with params: %s""" % (self._conf.getId(), self.getId(), str(paramsForLog(params))))
            answer = unicodeToUtf8(mcu.participant.add(params))
            Logger.get('CERNMCU').info("""Evt:%s, booking:%s, calling participant.add. Got answer: %s""" % (self._conf.getId(), self.getId(), str(answer)))

            if self._hasBeenStarted:
                #we have to connect the new participant
                params = MCUParams(conferenceName = self._bookingParams["name"],
                                   participantName = participant.getParticipantName())
                Logger.get('CERNMCU').info("""Evt:%s, booking:%s, calling participant.connect with params: %s""" % (self._conf.getId(), self.getId(), str(paramsForLog(params))))
                answer = unicodeToUtf8(mcu.participant.connect(params))
                Logger.get('CERNMCU').info("""Evt:%s, booking:%s, calling participant.connect. Got answer: %s""" % (self._conf.getId(), self.getId(), str(answer)))

            return True
        except Fault, e:
            Logger.get('CERNMCU').warning("""Evt:%s, calling participant.add. Got error: %s""" % (self._conf.getId(), str(e)))
            fault = self.handleFault('add', e)
            fault.setInfo(participant.getIp())
            return fault

    def modifyParticipantDisplayName(self, participantName, displayName):
        """ Modifies the display name of a participant
            returns: True if successful, a CERNMCUError if there is a problem in some cases, raises an Exception in others
        """
        try:
            mcu = MCU.getInstance()

            if self._hasBeenStarted:
                state = 'activeState'
            else:
                state = 'configuredState'

            params = MCUParticipantCommonParams(conferenceName = self._bookingParams["name"],
                                                participantName = participantName,
                                                displayNameOverrideValue = displayName,
                                                operationScope = state
                                                )
            Logger.get('CERNMCU').info("""Evt:%s, booking:%s, calling participant.modify with params: %s""" % (self._conf.getId(), self.getId(), str(paramsForLog(params))))
            answer = unicodeToUtf8(mcu.participant.modify(params))
            Logger.get('CERNMCU').info("""Evt:%s, booking:%s, calling participant.modify. Got answer: %s""" % (self._conf.getId(), self.getId(), str(answer)))

            return True
        except Fault, e:
            Logger.get('CERNMCU').warning("""Evt:%s, calling participant.modify. Got error: %s""" % (self._conf.getId(), str(e)))
            fault = self.handleFault('modifyParticipant', e)
            fault.setInfo(participantName)
            return fault

    def removeParticipant(self, participantName):
        """ Removes a participant to the MCU conference represented by this booking.
            participant: a participant name like the one returned by Participant.getParticipantName
            returns: True if successful, a CERNMCUError if there is a problem in some cases, raises an Exception in others
        """
        try:
            mcu = MCU.getInstance()

            params = MCUParams(conferenceName = self._bookingParams["name"],
                               participantName = participantName)
            Logger.get('CERNMCU').info("""Evt:%s, booking:%s, calling participant.remove with params: %s""" % (self._conf.getId(), self.getId(), str(paramsForLog(params))))
            answer = unicodeToUtf8(mcu.participant.remove(params))
            Logger.get('CERNMCU').info("""Evt:%s, booking:%s, calling participant.remove. Got answer: %s""" % (self._conf.getId(), self.getId(), str(answer)))
            return True
        except Fault, e:
            Logger.get('CERNMCU').warning("""Evt:%s, calling participant.remove. Got error: %s""" % (self._conf.getId(), str(e)))
            return self.handleFault('remove', e)

    def checkCanStart(self, changeMessage = True):
        if self._created:
            now = nowutc()
            self._canBeNotifiedOfEventDateChanges = CSBooking._canBeNotifiedOfEventDateChanges
            if self.getStartDate() < now and self.getEndDate() > now and not self._hasBeenStarted:
                self._canBeStarted = True
                self._canBeStopped = False
                if changeMessage:
                    self._statusMessage = _("Ready to start!")
                    self._statusClass = "statusMessageOK"
            else:
                self._canBeStarted = False
                if now > self.getEndDate() and changeMessage:
                    self._statusMessage = _("Already took place")
                    self._statusClass = "statusMessageOther"
                    self._needsToBeNotifiedOfDateChanges = False
                    self._canBeNotifiedOfEventDateChanges = False

    def queryConference(self):
        """ Queries the MCU for information about a conference with the same conferenceName as self._bookingParams["name"]
            If found, the attributes of the Indico booking object are updated, included the participants.
            If not found, an error message will appear and the conference will be marked as not created.
        """
        try:
            mcu = MCU.getInstance()

            enumerateID = None
            keepAsking = True
            found = False

            while keepAsking and not found:
                if enumerateID:
                    params = MCUParams(enumerateID = enumerateID)
                else:
                    params = MCUParams()

                Logger.get('CERNMCU').info("""Evt:%s, booking:%s, calling conference.enumerate with params: %s""" % (self._conf.getId(), self.getId(), str(paramsForLog(params))))
                answer = unicodeToUtf8(mcu.conference.enumerate(params))
                #un-comment to print all the garbage about other conferences received
                #Logger.get('CERNMCU').debug("""Evt:%s, booking:%s, calling conference.enumerate. Got answer: %s""" % (self._conf.getId(), self.getId(), str(answer)))

                for conference in answer.get("conferences", []):
                    if conference.get("conferenceName", None) == self._bookingParams["name"]:

                        Logger.get('CERNMCU').info("""Evt:%s, booking:%s, calling conference.enumerate. Found conference:\n%s""" % (self._conf.getId(), self.getId(), str(conference)))

                        found = True

                        remoteDescription = conference.get("description", '')
                        if unicodeLength(remoteDescription) < 31 or not self._bookingParams["description"].startswith(remoteDescription):
                            self._bookingParams["description"] = remoteDescription
                        self._bookingParams["id"] = conference.get("numericId", '')
                        if not self._autoGeneratedId:
                            self._customId = self._bookingParams["id"]
                        self._oldName = self._bookingParams["name"]
                        self._pin = conference.get("pin", '')

                        remoteParticipants = self.queryParticipants()
                        for id, participant in self._participants.items():
                            if participant.getParticipantName() not in remoteParticipants:
                                del self._participants[id]

                        for participant in self._participants.itervalues():
                            del remoteParticipants[participant.getParticipantName()]

                        for participantName, attributes in remoteParticipants.iteritems():
                            try:
                                possibleParticipantId, rest = participantName.split('b')[1:]
                                possibleBookingId, possibleConfId = rest.split('c')
                            except ValueError:
                                possibleParticipantId = None
                                possibleBookingId = None
                                possibleConfId = None

                            idIsNumeric = True
                            try:
                                int(possibleParticipantId)
                            except ValueError:
                                idIsNumeric = False

                            if idIsNumeric and possibleBookingId == self._id and possibleConfId == self.getConference().getId():
                                self._participants[id] = ParticipantRoom(self, possibleParticipantId,
                                                                         {"name": attributes["displayName"],
                                                                          "ip": attributes["ip"]}, True)
                            else:
                                self._participants[id] = ParticipantRoom(self, participantName,
                                                                         {"name": attributes["displayName"],
                                                                          "ip": attributes["ip"]}, False)


                        startTime = conference.get("startTime", None)
                        if startTime:
                            adjustedDate = setAdjustedDate(datetimeFromMCUTime(startTime), tz = getCERNMCUOptionValueByName("MCUTZ"))
                            self.setStartDate(adjustedDate)

                        durationSeconds = conference.get("durationSeconds", None)
                        if durationSeconds:
                            self.setDurationSeconds(durationSeconds)
                        else:
                            self.setEndDate(self.getStartDate())

                        self._created = True
                        break

                enumerateID = answer.get("enumerateID", None)
                keepAsking = enumerateID is not None

            if not found:
                self._created = False

            self._p_changed = 1

        except Fault, e:
            Logger.get('CERNMCU').warning("""Evt:%s, booking:%s, calling participants.enumerate. Got error: %s""" % (self._conf.getId(), self.getId(), str(e)))
            raise e
        except socket.error, e:
            handleSocketError(e)

    def queryParticipants(self):
        """ Queries the MCU for the list of participants of this conference,
            using participant.enumerate.
            Returns a list of Participant objects
        """
        try:
            mcu = MCU.getInstance()
            participants = {} #key = participantName, value = {displayName, ip}

            enumerateID = None
            keepAsking = True

            while keepAsking:
                if enumerateID:
                    params = MCUParams(enumerateID = enumerateID)
                else:
                    params = MCUParams()

                Logger.get('CERNMCU').info("""Evt:%s, booking:%s, calling participants.enumerate with params: %s""" % (self._conf.getId(), self.getId(), str(paramsForLog(params))))
                answer = unicodeToUtf8(mcu.participant.enumerate(params))
                #un-comment to print all the garbage received about other participants
                #Logger.get('CERNMCU').debug("""Evt:%s, booking:%s, calling participants.enumerate. Got answer: %s""" % (self._conf.getId(), self.getId(), str(answer)))

                for participant in answer.get("participants", []):
                    if participant.get("conferenceName", None) == self._bookingParams["name"]:
                        Logger.get('CERNMCU').info("""Evt:%s, booking:%s, calling participants.enumerate. Found participant:\n%s""" % (self._conf.getId(), self.getId(), str(participant)))
                        name = participant.get("participantName", None)
                        displayName = participant.get("displayName", None)
                        ip = participant.get("address", None)
                        if name:
                            participants[name] = {"displayName": displayName, "ip": ip}

                enumerateID = answer.get("enumerateID", None)
                keepAsking = enumerateID is not None

            return participants


        except Fault, e:
            Logger.get('CERNMCU').warning("""Evt:%s, booking:%s, calling participants.enumerate. Got error: %s""" % (self._conf.getId(), self.getId(), str(e)))
            raise e
        except socket.error, e:
            handleSocketError(e)

    def handleFault(self, operation, e):
        self._faultCode = e.faultCode
        self._faultString = e.faultString

        if e.faultCode == 14: #authorization error
            raise CERNMCUException(_("Authorization Error while Indico tried to connect to the MCU.\nPlease report to Indico support."), e)

        if operation == 'create' or operation == 'modify':
            if e.faultCode == 2: #duplicated name
                fault = CERNMCUError(e.faultCode)
                return fault
            if e.faultCode == 6: #too many conferences in MCU, no more can be created
                fault = CERNMCUError(e.faultCode)
                return fault
            elif e.faultCode == 18:  #duplicated ID
                if self._autoGeneratedId:
                    self._creationTriesCounter = self._creationTriesCounter + 1
                    return self._create()
                else:
                    fault = CERNMCUError(e.faultCode)
                    return fault
            else: #another error
                raise CERNMCUException(_("Problem with the MCU while creating or modifying a conference"), e)

        elif operation == 'delete':
            raise CERNMCUException(_("Problem with the MCU while removing a conference"), e)

        elif operation == 'add':
            if e.faultCode == 3: #duplicate participant name
                fault = CERNMCUError(e.faultCode)
                return fault
            if e.faultCode == 7: #too many participants in MCU, no more can be created
                fault = CERNMCUError(e.faultCode)
                return fault
            else:
                raise CERNMCUException(_("Problem with the MCU while adding a participant"), e)

        elif operation == 'modifyParticipant':
            raise CERNMCUException(_("Problem with the MCU while modifying the name of a participant"), e)

        elif operation == 'remove':
            raise CERNMCUException(_("Problem with the MCU while removing a participant"), e)

        elif operation == 'start':
            raise CERNMCUException(_("Problem with the MCU while starting a conference"), e)

        elif operation == 'stop':
            if e.faultCode == 201: #we tried to disconnect a participant that was not connected
                return None
            elif e.faultCode == 5: #we tried to disconnect a participant that didn't exist
                return None
            else:
                raise CERNMCUException(_("Problem with the MCU while stopping a conference"), e)

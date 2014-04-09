# -*- coding: utf-8 -*-
##
##
## This file is part of Indico.
## Copyright (C) 2002 - 2014 European Organization for Nuclear Research (CERN).
##
## Indico is free software; you can redistribute it and/or
## modify it under the terms of the GNU General Public License as
## published by the Free Software Foundation; either version 3 of the
## License, or (at your option) any later version.
##
## Indico is distributed in the hope that it will be useful, but
## WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
## General Public License for more details.
##
## You should have received a copy of the GNU General Public License
## along with Indico;if not, see <http://www.gnu.org/licenses/>.

from flask import request
from wtforms import Form, validators, TextField, PasswordField, BooleanField

import MaKaC.webinterface.rh.base as base
import MaKaC.webinterface.pages.wizard as wizard
import MaKaC.user as user
import MaKaC.webinterface.pages.signIn as signIn
from MaKaC.common.info import HelperMaKaCInfo
from MaKaC.user import AvatarHolder
from MaKaC.errors import AccessError, FormValuesError
from MaKaC.authentication import AuthenticatorMgr
from MaKaC.accessControl import AdminList
from indico.web.forms.validators import UsedIfChecked
from MaKaC.i18n import _


class RHWizard(base.RHDisplayBaseProtected):

    def _setUserData(self, av):
        av.setName(self._params["name"])
        av.setSurName(self._params["surname"])
        av.setOrganisation(self._params["organisation"])
        av.setEmail(self._params["userEmail"])
        av.setTimezone(self._params["timezone"])
        av.setLang(self._params["lang"])

    def _checkProtection(self):
        minfo = HelperMaKaCInfo.getMaKaCInfoInstance()
        if minfo.getAdminList().getList() or AvatarHolder()._getIdx():
            raise AccessError

    def _checkParams_GET(self):
        self._params = request.form.copy()

    def _checkParams_POST(self):
        self._params = request.form.copy()
        base.RHDisplayBaseProtected._checkParams(self, self._params)
        self._enable = self._params.get("enable", "")

    def _process_GET(self):
        p = wizard.WPWizard(self, self._params)
        return p.display()

    def _process_POST(self):
        regform = RegistrationForm(request.form)
        if not regform.validate():
            raise FormValuesError(_("Some fields are invalid. Please, correct them and submit the form again."))
        else:
            # Creating new user
            ah = user.AvatarHolder()
            av = user.Avatar()
            authManager = AuthenticatorMgr()
            self._setUserData(av)
            ah.add(av)
            li = user.LoginInfo(self._params["login"], self._params["password"].encode('UTF8'))
            identity = authManager.createIdentity(li, av, "Local")
            authManager.add(identity)
            # Activating new account
            av.activateAccount()
            # Granting admin priviledges
            al = AdminList().getInstance()
            al.grant(av)
            # Configuring server's settings
            minfo = HelperMaKaCInfo.getMaKaCInfoInstance()
            minfo.setOrganisation(self._params["organisation"])
            minfo.setTimezone(self._params["timezone"])
            minfo.setLang(self._params["lang"])
            minfo.setInstanceTrackingActive(bool(self._enable))
            if self._enable:
                minfo.setInstanceTrackingEmail(self._params["instanceTrackingEmail"])

            p = signIn.WPAdminCreated(self, av)
            return p.display()


class RegistrationForm(Form):

    name = TextField('Username', [validators.Required()])
    surname = TextField('Surname', [validators.Required()])
    userEmail = TextField('User Email Address', [validators.Required(), validators.Email()])
    login = TextField('Login', [validators.Required()])
    password = PasswordField('New Password', [validators.Required()])
    passwordBis = PasswordField('Repeat Password', [validators.EqualTo('password', message='Passwords must match')])
    organisation = TextField('Organisation', [validators.Required()])
    enable = BooleanField('Enable Instance Tracking')
    instanceTrackingEmail = TextField('Instance Tracking Email Address',
                                      [UsedIfChecked('enable'), validators.Required(), validators.Email()])
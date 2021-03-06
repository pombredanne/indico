# This file is part of Indico.
# Copyright (C) 2002 - 2016 European Organization for Nuclear Research (CERN).
#
# Indico is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 3 of the
# License, or (at your option) any later version.
#
# Indico is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Indico; if not, see <http://www.gnu.org/licenses/>.

from __future__ import unicode_literals

from collections import defaultdict, OrderedDict
from datetime import timedelta
from io import BytesIO
from operator import attrgetter

from flask import flash, session
from pytz import timezone
from sqlalchemy.orm import load_only, contains_eager, noload, joinedload, subqueryload

from indico.core.db import db
from indico.modules.events.models.events import Event
from indico.modules.events.models.persons import EventPerson
from indico.modules.events.contributions.models.contributions import Contribution
from indico.modules.events.contributions.models.subcontributions import SubContribution
from indico.modules.events.contributions.models.persons import ContributionPersonLink, SubContributionPersonLink
from indico.modules.events.contributions.models.principals import ContributionPrincipal
from indico.modules.events.util import serialize_person_link, ListGeneratorBase
from indico.modules.attachments.util import get_attached_items
from indico.util.caching import memoize_request
from indico.util.date_time import format_human_timedelta, format_datetime
from indico.util.i18n import _
from indico.util.string import to_unicode
from indico.util.user import iter_acl
from indico.web.flask.templating import get_template_module
from indico.web.flask.util import url_for
from indico.web.http_api.metadata.serializer import Serializer
from indico.web.util import jsonify_data
from MaKaC.common.timezoneUtils import DisplayTZ


def get_events_with_linked_contributions(user, from_dt=None, to_dt=None):
    """Returns a dict with keys representing event_id and the values containing
    data about the user rights for contributions within the event

    :param user: A `User`
    :param from_dt: The earliest event start time to look for
    :param to_dt: The latest event start time to look for
    """
    def add_acl_data():
        query = (user.in_contribution_acls
                 .options(load_only('contribution_id', 'roles', 'full_access', 'read_access'))
                 .options(noload('*'))
                 .options(contains_eager(ContributionPrincipal.contribution).load_only('event_id'))
                 .join(Contribution)
                 .join(Event, Event.id == Contribution.event_id)
                 .filter(~Contribution.is_deleted, ~Event.is_deleted, Event.starts_between(from_dt, to_dt)))
        for principal in query:
            roles = data[principal.contribution.event_id]
            if 'submit' in principal.roles:
                roles.add('contribution_submission')
            if principal.full_access:
                roles.add('contribution_manager')
            if principal.read_access:
                roles.add('contribution_access')

    def add_contrib_data():
        has_contrib = (EventPerson.contribution_links.any(
            ContributionPersonLink.contribution.has(~Contribution.is_deleted)))
        has_subcontrib = EventPerson.subcontribution_links.any(
            SubContributionPersonLink.subcontribution.has(db.and_(
                ~SubContribution.is_deleted,
                SubContribution.contribution.has(~Contribution.is_deleted))))
        query = (Event.query
                 .options(load_only('id'))
                 .options(noload('*'))
                 .filter(~Event.is_deleted,
                         Event.starts_between(from_dt, to_dt),
                         Event.persons.any((EventPerson.user_id == user.id) & (has_contrib | has_subcontrib))))
        for event in query:
            data[event.id].add('contributor')

    data = defaultdict(set)
    add_acl_data()
    add_contrib_data()
    return data


def serialize_contribution_person_link(person_link, is_submitter=None):
    """Serialize ContributionPersonLink to JSON-like object"""
    data = serialize_person_link(person_link)
    data['isSpeaker'] = person_link.is_speaker
    if not isinstance(person_link, SubContributionPersonLink):
        data['authorType'] = person_link.author_type.value
        data['isSubmitter'] = person_link.is_submitter if is_submitter is None else is_submitter
    return data


class ContributionListGenerator(ListGeneratorBase):
    """Listing and filtering actions in the contribution list."""

    endpoint = '.manage_contributions'
    list_link_type = 'contribution'

    def __init__(self, event):
        super(ContributionListGenerator, self).__init__(event)
        self.default_list_config = {'filters': {'items': {}}}

        session_empty = {None: 'No session'}
        track_empty = {None: 'No track'}
        type_empty = {None: 'No type'}
        session_choices = {unicode(s.id): s.title for s in self.list_event.sessions}
        track_choices = {unicode(t.id): to_unicode(t.getTitle()) for t in self.list_event.as_legacy.getTrackList()}
        type_choices = {unicode(t.id): t.name for t in self.list_event.contribution_types}
        self.static_items = OrderedDict([
            ('session', {'title': _('Session'),
                         'filter_choices': OrderedDict(session_empty.items() + session_choices.items())}),
            ('track', {'title': _('Track'),
                       'filter_choices': OrderedDict(track_empty.items() + track_choices.items())}),
            ('type', {'title': _('Type'),
                      'filter_choices': OrderedDict(type_empty.items() + type_choices.items())}),
            ('status', {'title': _('Status'), 'filter_choices': {'scheduled': _('Scheduled'),
                                                                 'unscheduled': _('Not scheduled')}})
        ])

        self.list_config = self._get_config()

    def build_query(self):
        timetable_entry_strategy = joinedload('timetable_entry')
        timetable_entry_strategy.lazyload('*')
        return (Contribution.query.with_parent(self.list_event)
                .order_by(Contribution.friendly_id)
                .options(timetable_entry_strategy,
                         joinedload('session'),
                         subqueryload('person_links'),
                         db.undefer('subcontribution_count'),
                         db.undefer('attachment_count'),
                         db.undefer('is_scheduled')))

    def _filter_list_entries(self, query, filters):
        if not filters.get('items'):
            return query
        criteria = []
        if 'status' in filters['items']:
            filtered_statuses = filters['items']['status']
            status_criteria = []
            if 'scheduled' in filtered_statuses:
                status_criteria.append(Contribution.is_scheduled)
            if 'unscheduled' in filtered_statuses:
                status_criteria.append(~Contribution.is_scheduled)
            if status_criteria:
                criteria.append(db.or_(*status_criteria))

        filter_cols = {'session': Contribution.session_id,
                       'track': Contribution.track_id,
                       'type': Contribution.type_id}
        for key, column in filter_cols.iteritems():
            ids = set(filters['items'].get(key, ()))
            if not ids:
                continue
            column_criteria = []
            if None in ids:
                column_criteria.append(column.is_(None))
            if ids - {None}:
                column_criteria.append(column.in_(ids - {None}))
            criteria.append(db.or_(*column_criteria))
        return query.filter(*criteria)

    def get_list_kwargs(self):
        contributions_query = self.build_query()
        total_entries = contributions_query.count()
        contributions = self._filter_list_entries(contributions_query, self.list_config['filters']).all()
        sessions = [{'id': s.id, 'title': s.title, 'colors': s.colors} for s in self.list_event.sessions]
        tracks = [{'id': int(t.id), 'title': to_unicode(t.getTitle())}
                  for t in self.list_event.as_legacy.getTrackList()]
        total_duration = (sum((c.duration for c in contributions), timedelta()),
                          sum((c.duration for c in contributions if c.timetable_entry), timedelta()))
        return {'contribs': contributions, 'sessions': sessions, 'tracks': tracks, 'total_entries': total_entries,
                'total_duration': total_duration}

    def render_list(self, contrib=None):
        """Render the contribution list template components.

        :param contrib: Used in RHs responsible for CRUD operations on a
                        contribution.
        :return: dict containing the list's entries, the fragment of
                 displayed entries and whether the contrib passed is displayed
                 in the results.
        """
        contrib_list_kwargs = self.get_list_kwargs()
        total_entries = contrib_list_kwargs.pop('total_entries')
        tpl_contrib = get_template_module('events/contributions/management/_contribution_list.html')
        tpl_lists = get_template_module('events/management/_lists.html')
        contribs = contrib_list_kwargs['contribs']
        filter_statistics = tpl_lists.render_filter_statistics(len(contribs), total_entries,
                                                               contrib_list_kwargs.pop('total_duration'))
        return {'html': tpl_contrib.render_contrib_list(self.list_event, total_entries, **contrib_list_kwargs),
                'hide_contrib': contrib not in contribs if contrib else None,
                'filter_statistics': filter_statistics}

    def flash_info_message(self, contrib):
        flash(_("The contribution '{}' is not displayed in the list due to the enabled filters")
              .format(contrib.title), 'info')


def generate_spreadsheet_from_contributions(contributions):
    """Return a tuple consisting of spreadsheet columns and respective
    contribution values"""

    headers = ['Id', 'Title', 'Description', 'Date', 'Duration', 'Type', 'Session', 'Track', 'Presenters', 'Materials']
    rows = []
    for c in sorted(contributions, key=attrgetter('friendly_id')):
        contrib_data = {'Id': c.friendly_id, 'Title': c.title, 'Description': c.description,
                        'Duration': format_human_timedelta(c.duration),
                        'Date': format_datetime(c.timetable_entry.start_dt) if c.timetable_entry else None,
                        'Type': c.type.name if c.type else None,
                        'Session': c.session.title if c.session else None,
                        'Track': c.track.title if c.track else None,
                        'Materials': None,
                        'Presenters': ', '.join(speaker.person.full_name for speaker in c.speakers)}

        attachments = []
        attached_items = get_attached_items(c)
        for attachment in attached_items.get('files', []):
            attachments.append(attachment.absolute_download_url)

        for folder in attached_items.get('folders', []):
            for attachment in folder.attachments:
                attachments.append(attachment.absolute_download_url)

        if attachments:
            contrib_data['Materials'] = ', '.join(attachments)
        rows.append(contrib_data)
    return headers, rows


def make_contribution_form(event):
    """Extends the contribution WTForm to add the extra fields.

    Each extra field will use a field named ``custom_ID``.

    :param event: The `Event` for which to create the contribution form.
    :return: A `ContributionForm` subclass.
    """
    from indico.modules.events.contributions.forms import ContributionForm

    form_class = type(b'_ContributionForm', (ContributionForm,), {})
    for custom_field in event.contribution_fields:
        field_impl = custom_field.mgmt_field
        if field_impl is None:
            # field definition is not available anymore
            continue
        name = 'custom_{}'.format(custom_field.id)
        setattr(form_class, name, field_impl.create_wtf_field())
    return form_class


def contribution_type_row(contrib_type):
    template = get_template_module('events/contributions/management/_types_table.html')
    html = template.types_table_row(contrib_type=contrib_type)
    return jsonify_data(html_row=html, flash=False)


@memoize_request
def get_contributions_with_user_as_submitter(event, user):
    """Get a list of contributions in which the `user` has submission rights"""
    contribs = (Contribution.query.with_parent(event)
                .options(joinedload('acl_entries'))
                .filter(Contribution.acl_entries.any(ContributionPrincipal.has_management_role('submit')))
                .all())
    return {c for c in contribs if any(user in entry.principal for entry in iter_acl(c.acl_entries))}


def serialize_contribution_for_ical(contrib):
    return {
        '_fossil': 'contributionMetadata',
        'id': contrib.id,
        'startDate': contrib.timetable_entry.start_dt if contrib.timetable_entry else None,
        'endDate': contrib.timetable_entry.end_dt if contrib.timetable_entry else None,
        'url': url_for('contributions.display_contribution', contrib, _external=True),
        'title': contrib.title,
        'location': contrib.venue_name,
        'roomFullname': contrib.room_name,
        'speakers': [serialize_person_link(x) for x in contrib.speakers],
        'description': contrib.description
    }


def get_contribution_ical_file(contrib):
    data = {'results': serialize_contribution_for_ical(contrib)}
    serializer = Serializer.create('ics')
    return BytesIO(serializer(data))


class ContributionDisplayListGenerator(ContributionListGenerator):
    endpoint = '.contribution_list'
    list_link_type = 'contribution_display'

    def render_contribution_list(self):
        """Render the contribution list template components.

        :return: dict containing the list's entries, the fragment of
                 displayed entries and whether the contrib passed is displayed
                 in the results.
        """
        contrib_list_kwargs = self.get_list_kwargs()
        total_entries = contrib_list_kwargs.pop('total_entries')
        contribs = contrib_list_kwargs['contribs']
        tpl = get_template_module('events/contributions/display/_contribution_list.html')
        tpl_lists = get_template_module('events/management/_lists.html')
        tz = timezone(DisplayTZ(session.user, self.list_event.as_legacy).getDisplayTZ())
        return {'html': tpl.render_contribution_list(self.list_event, tz, contribs),
                'counter': tpl_lists.render_displayed_entries_fragment(len(contribs), total_entries)}

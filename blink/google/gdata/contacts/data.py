#                                                                       
# Copyright (C) 2009 Google Inc.                                        
#                                                                       
# Licensed under the Apache License, Version 2.0 (the "License");       
# you may not use this file except in compliance with the License.      
# You may obtain a copy of the License at                               
#                                                                       
#      http://www.apache.org/licenses/LICENSE-2.0                       
#                                                                       
# Unless required by applicable law or agreed to in writing, software   
# distributed under the License is distributed on an "AS IS" BASIS,     
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and     
# limitations under the License.                                          

"""Data model classes for parsing and generating XML for the Contacts API."""


__author__ = 'vinces1979@gmail.com (Vince Spicer)'

from blink.google.atom import core as atom_core
from blink.google.atom import data as atom_data
from blink.google import gdata
from blink.google.gdata import data as gdata_data


PHOTO_LINK_REL = 'http://schemas.google.com/contacts/2008/rel#photo'
PHOTO_EDIT_LINK_REL = 'http://schemas.google.com/contacts/2008/rel#edit-photo'

EXTERNAL_ID_ORGANIZATION = 'organization'

RELATION_MANAGER = 'manager'

CONTACTS_NAMESPACE = 'http://schemas.google.com/contact/2008'
CONTACTS_TEMPLATE = '{%s}%%s' % CONTACTS_NAMESPACE


class BillingInformation(atom_core.XmlElement):
  """ 
  gContact:billingInformation
  Specifies billing information of the entity represented by the contact. The element cannot be repeated. 
  """
  
  _qname = CONTACTS_TEMPLATE % 'billingInformation'


class Birthday(atom_core.XmlElement):
  """ 
 Stores birthday date of the person represented by the contact. The element cannot be repeated. 
 """
  
  _qname = CONTACTS_TEMPLATE % 'birthday'
  when = 'when'


class CalendarLink(atom_core.XmlElement):
  """ 
  Storage for URL of the contact's calendar. The element can be repeated. 
  """
  
  _qname = CONTACTS_TEMPLATE % 'calendarLink'
  rel = 'rel'
  label = 'label'
  primary = 'primary'
  href = 'href'


class DirectoryServer(atom_core.XmlElement):
  """ 
  A directory server associated with this contact. 
  May not be repeated. 
  """
  
  _qname = CONTACTS_TEMPLATE % 'directoryServer'


class Event(atom_core.XmlElement):
  """
  These elements describe events associated with a contact. 
  They may be repeated
  """
  
  _qname = CONTACTS_TEMPLATE % 'event'
  label = 'label'
  rel = 'rel'
  when = gdata_data.When


class ExternalId(atom_core.XmlElement):
  """
   Describes an ID of the contact in an external system of some kind. 
  This element may be repeated. 
  """
  
  _qname = CONTACTS_TEMPLATE % 'externalId'


def ExternalIdFromString(xml_string):
  return atom_core.parse(ExternalId, xml_string)


class Gender(atom_core.XmlElement):
  """ 
  Specifies the gender of the person represented by the contact.
  The element cannot be repeated. 
  """
  
  _qname = CONTACTS_TEMPLATE % 'directoryServer'
  value = 'value'


class Hobby(atom_core.XmlElement):
  """ 
  Describes an ID of the contact in an external system of some kind. 
  This element may be repeated. 
  """
  
  _qname = CONTACTS_TEMPLATE % 'hobby'


class Initials(atom_core.XmlElement):
  """ Specifies the initials of the person represented by the contact. The 
  element cannot be repeated. """
  
  _qname = CONTACTS_TEMPLATE % 'initials'


class Jot(atom_core.XmlElement):
  """ 
  Storage for arbitrary pieces of information about the contact. Each jot 
  has a type specified by the rel attribute and a text value. 
  The element can be repeated. 
  """
  
  _qname = CONTACTS_TEMPLATE % 'jot'
  rel = 'rel'


class Language(atom_core.XmlElement):
  """ 
 Specifies the preferred languages of the contact. 
 The element can be repeated.

  The language must be specified using one of two mutually exclusive methods: 
  using the freeform @label attribute, or using the @code attribute, whose value 
  must conform to the IETF BCP 47 specification.
  """
  
  _qname = CONTACTS_TEMPLATE % 'language'
  code = 'code'
  label = 'label'


class MaidenName(atom_core.XmlElement):
  """ 
  Specifies maiden name of the person represented by the contact. 
  The element cannot be repeated.
  """
  
  _qname = CONTACTS_TEMPLATE % 'maidenName'


class Mileage(atom_core.XmlElement):
  """ 
  Specifies the mileage for the entity represented by the contact. 
  Can be used for example to document distance needed for reimbursement 
  purposes. The value is not interpreted. The element cannot be repeated.
  """
  
  _qname = CONTACTS_TEMPLATE % 'mileage'


class NickName(atom_core.XmlElement):
  """
  Specifies the nickname of the person represented by the contact. 
  The element cannot be repeated.
  """
  
  _qname = CONTACTS_TEMPLATE % 'nickname'


class Occupation(atom_core.XmlElement):
  """
  Specifies the occupation/profession of the person specified by the contact. 
  The element cannot be repeated.
  """
  
  _qname = CONTACTS_TEMPLATE % 'occupation'


class Priority(atom_core.XmlElement):
  """ 
  Classifies importance of the contact into 3 categories:
    * Low
    * Normal
    * High

  The priority element cannot be repeated. 
  """

  _qname = CONTACTS_TEMPLATE % 'priority'


class Relation(atom_core.XmlElement):
  """
  This element describe another entity (usually a person) that is in a 
  relation of some kind with the contact.
  """

  _qname = CONTACTS_TEMPLATE % 'relation'
  rel = 'rel'
  label = 'label'


class Sensitivity(atom_core.XmlElement):
  """
  Classifies sensitivity of the contact into the following categories:
    * Confidential
    * Normal
    * Personal
    * Private

  The sensitivity element cannot be repeated. 
  """

  _qname = CONTACTS_TEMPLATE % 'sensitivity'
  rel = 'rel'


class UserDefinedField(atom_core.XmlElement):
  """
  Represents an arbitrary key-value pair attached to the contact.
  """

  _qname = CONTACTS_TEMPLATE % 'userDefinedField'
  key = 'key'
  value = 'value'


def UserDefinedFieldFromString(xml_string):
  return atom_core.parse(UserDefinedField, xml_string)


class Website(atom_core.XmlElement):
  """
  Describes websites associated with the contact, including links. 
  May be repeated.
  """

  _qname = CONTACTS_TEMPLATE % 'website'
  
  href = 'href'
  label = 'label'
  primary = 'primary'
  rel = 'rel'


def WebsiteFromString(xml_string):
  return atom_core.parse(Website, xml_string)


class HouseName(atom_core.XmlElement):
  """
  Used in places where houses or buildings have names (and 
  not necessarily numbers), eg. "The Pillars".
  """
  
  _qname = CONTACTS_TEMPLATE % 'housename'


class Street(atom_core.XmlElement):
  """
  Can be street, avenue, road, etc. This element also includes the house 
  number and room/apartment/flat/floor number.
  """
  
  _qname = CONTACTS_TEMPLATE % 'street'


class POBox(atom_core.XmlElement):
  """
  Covers actual P.O. boxes, drawers, locked bags, etc. This is usually but not
  always mutually exclusive with street
  """
  
  _qname = CONTACTS_TEMPLATE % 'pobox'


class Neighborhood(atom_core.XmlElement):
  """
  This is used to disambiguate a street address when a city contains more than
  one street with the same name, or to specify a small place whose mail is
  routed through a larger postal town. In China it could be a county or a 
  minor city.
  """
  
  _qname = CONTACTS_TEMPLATE % 'neighborhood'


class City(atom_core.XmlElement):
  """
  Can be city, village, town, borough, etc. This is the postal town and not
  necessarily the place of residence or place of business.
  """
  
  _qname = CONTACTS_TEMPLATE % 'city'


class SubRegion(atom_core.XmlElement):
  """
  Handles administrative districts such as U.S. or U.K. counties that are not
   used for mail addressing purposes. Subregion is not intended for 
   delivery addresses.
  """

  _qname = CONTACTS_TEMPLATE % 'subregion'


class Region(atom_core.XmlElement):
  """
  A state, province, county (in Ireland), Land (in Germany), 
  departement (in France), etc.
  """

  _qname = CONTACTS_TEMPLATE % 'region'
  

class PostalCode(atom_core.XmlElement):
  """
  Postal code. Usually country-wide, but sometimes specific to the 
  city (e.g. "2" in "Dublin 2, Ireland" addresses).
  """
  
  _qname = CONTACTS_TEMPLATE % 'postcode'


class Country(atom_core.XmlElement):
  """ The name or code of the country. """

  _qname = CONTACTS_TEMPLATE % 'country'  


class PersonEntry(gdata_data.BatchEntry):
  """Represents a google contact"""

  billing_information = BillingInformation
  birthday = Birthday
  calendar_link = [CalendarLink]
  directory_server = DirectoryServer
  event = [Event]
  external_id = [ExternalId]
  gender = Gender
  hobby = [Hobby]
  initals = Initials
  jot = [Jot]
  language= [Language]
  maiden_name = MaidenName
  mileage = Mileage
  nickname = NickName
  occupation = Occupation
  priority = Priority
  relation = [Relation]
  sensitivity = Sensitivity
  user_defined_field = [UserDefinedField]
  website = [Website]
  
  name = gdata_data.Name
  phone_number = [gdata_data.PhoneNumber]
  organization = gdata_data.Organization
  postal_address = [gdata_data.PostalAddress]
  email = [gdata_data.Email]
  im = [gdata_data.Im]
  structured_postal_address = [gdata_data.StructuredPostalAddress]
  extended_property = [gdata_data.ExtendedProperty]
  

class Deleted(atom_core.XmlElement):
  """If present, indicates that this contact has been deleted."""
  _qname = gdata.GDATA_TEMPLATE % 'deleted'


class GroupMembershipInfo(atom_core.XmlElement):
  """
  Identifies the group to which the contact belongs or belonged.
  The group is referenced by its id.
  """

  _qname = CONTACTS_TEMPLATE % 'groupMembershipInfo'

  href = 'href'
  deleted = 'deleted'


class ContactEntry(PersonEntry):
  """A Google Contacts flavor of an Atom Entry."""

  deleted = Deleted
  group_membership_info = [GroupMembershipInfo]
  organization = gdata_data.Organization

  def GetPhotoLink(self):
    for a_link in self.link:
      if a_link.rel == PHOTO_LINK_REL:
        return a_link
    return None

  def GetPhotoEditLink(self):
    for a_link in self.link:
      if a_link.rel == PHOTO_EDIT_LINK_REL:
        return a_link
    return None

  def get_entry_photo_data(self):
    photo = self.GetPhotoLink()
    if photo._other_attributes.get('{http://schemas.google.com/g/2005}etag'):
      return (photo.href, photo._other_attributes.get('{http://schemas.google.com/g/2005}etag').strip('"'))
    return (None, None)


class ContactsFeed(gdata_data.BatchFeed):
  """A collection of Contacts."""
  entry = [ContactEntry]


class SystemGroup(atom_core.XmlElement):
  """The contacts systemGroup element.
  
  When used within a contact group entry, indicates that the group in
  question is one of the predefined system groups."""

  _qname = CONTACTS_TEMPLATE % 'systemGroup'
  id = 'id'


class GroupEntry(gdata_data.BatchEntry):
  """Represents a contact group."""
  extended_property = [gdata_data.ExtendedProperty]
  system_group = SystemGroup


class GroupsFeed(gdata_data.BatchFeed):
  """A Google contact groups feed flavor of an Atom Feed."""
  entry = [GroupEntry]


class ProfileEntry(PersonEntry):
  """A Google Profiles flavor of an Atom Entry."""


def ProfileEntryFromString(xml_string):
  """Converts an XML string into a ProfileEntry object.

  Args:
    xml_string: string The XML describing a Profile entry.

  Returns:
    A ProfileEntry object corresponding to the given XML.
  """
  return atom_core.parse(ProfileEntry, xml_string)


class ProfilesFeed(gdata_data.BatchFeed):
  """A Google Profiles feed flavor of an Atom Feed."""
  _qname = atom_data.ATOM_TEMPLATE % 'feed'
  entry = [ProfileEntry]


def ProfilesFeedFromString(xml_string):
  """Converts an XML string into a ProfilesFeed object.

  Args:
    xml_string: string The XML describing a Profiles feed.

  Returns:
    A ProfilesFeed object corresponding to the given XML.
  """
  return atom_core.parse(ProfilesFeed, xml_string)



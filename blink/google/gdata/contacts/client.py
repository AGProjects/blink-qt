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
from types import ListType, DictionaryType


"""Contains a client to communicate with the Contacts servers.

For documentation on the Contacts API, see:
http://code.google.com/apis/contatcs/
"""

__author__ = 'vinces1979@gmail.com (Vince Spicer)'

from blink.google import gdata
from blink.google.gdata import client as gdata_client, gauth as gdata_gauth
from blink.google.gdata.contacts import data as gdata_contacts_data


class ContactsClient(gdata_client.GDClient):
  api_version = '3'
  auth_service = 'cp'
  server = "www.google.com"
  contact_list = "default"
  auth_scopes = gdata_gauth.AUTH_SCOPES['cp']

  def get_feed_uri(self, kind='contacts', contact_list=None, projection='full',
                  scheme="http"):
    """Builds a feed URI.

    Args:
      kind: The type of feed to return, typically 'groups' or 'contacts'.
        Default value: 'contacts'.
      contact_list: The contact list to return a feed for.
        Default value: self.contact_list.
      projection: The projection to apply to the feed contents, for example
        'full', 'base', 'base/12345', 'full/batch'. Default value: 'full'.
      scheme: The URL scheme such as 'http' or 'https', None to return a
          relative URI without hostname.

    Returns:
      A feed URI using the given kind, contact list, and projection.
      Example: '/m8/feeds/contacts/default/full'.
    """
    contact_list = contact_list or self.contact_list
    if kind == 'profiles':
      contact_list = 'domain/%s' % contact_list
    prefix = scheme and '%s://%s' % (scheme, self.server) or ''
    return '%s/m8/feeds/%s/%s/%s' % (prefix, kind, contact_list, projection)

  GetFeedUri = get_feed_uri

  def get_contact(self, uri, desired_class=gdata_contacts_data.ContactEntry,
                  auth_token=None, **kwargs):
    return self.get_feed(uri, auth_token=auth_token, 
                         desired_class=desired_class, **kwargs)


  GetContact = get_contact


  def create_contact(self, new_contact, insert_uri=None,  auth_token=None,  **kwargs):
    """Adds an new contact to Google Contacts.

    Args:
      new_contact: atom.Entry or subclass A new contact which is to be added to
                Google Contacts.
      insert_uri: the URL to post new contacts to the feed
      url_params: dict (optional) Additional URL parameters to be included
                  in the insertion request.
      escape_params: boolean (optional) If true, the url_parameters will be
                     escaped before they are included in the request.

    Returns:
      On successful insert,  an entry containing the contact created
      On failure, a RequestError is raised of the form:
        {'status': HTTP status code from server,
         'reason': HTTP reason from the server,
         'body': HTTP body of the server's response}
    """
    insert_uri = insert_uri or self.GetFeedUri()
    return self.Post(new_contact, insert_uri, 
                     auth_token=auth_token,  **kwargs)

  CreateContact = create_contact

  def add_contact(self, new_contact, insert_uri=None, auth_token=None,  
                  billing_information=None, birthday=None, calendar_link=None, **kwargs):
    """Adds an new contact to Google Contacts.

    Args:
      new_contact: atom.Entry or subclass A new contact which is to be added to
                Google Contacts.
      insert_uri: the URL to post new contacts to the feed
      url_params: dict (optional) Additional URL parameters to be included
                  in the insertion request.
      escape_params: boolean (optional) If true, the url_parameters will be
                     escaped before they are included in the request.

    Returns:
      On successful insert,  an entry containing the contact created
      On failure, a RequestError is raised of the form:
        {'status': HTTP status code from server,
         'reason': HTTP reason from the server,
         'body': HTTP body of the server's response}
    """
    
    contact = gdata_contacts_data.ContactEntry()
    
    if billing_information is not None:
      if not isinstance(billing_information, gdata_contacts_data.BillingInformation):
        billing_information = gdata_contacts_data.BillingInformation(text=billing_information) 
      
      contact.billing_information = billing_information

    if birthday is not None:
      if not isinstance(birthday, gdata_contacts_data.Birthday):
        birthday = gdata_contacts_data.Birthday(when=birthday)
      
      contact.birthday = birthday 
    
    if calendar_link is not None:
      if type(calendar_link) is not ListType:
        calendar_link = [calendar_link]
      
      for link in calendar_link:
        if not isinstance(link, gdata_contacts_data.CalendarLink):
          if type(link) is not DictionaryType:
            raise TypeError, "calendar_link Requires dictionary not %s" % type(link)
        
          link = gdata_contacts_data.CalendarLink(
                                                  rel=link.get("rel", None),
                                                  label=link.get("label", None),
                                                  primary=link.get("primary", None),
                                                  href=link.get("href", None),
                                                  )
         
        contact.calendar_link.append(link)
    
    insert_uri = insert_uri or self.GetFeedUri()
    return self.Post(contact, insert_uri, 
                     auth_token=auth_token,  **kwargs)

  AddContact = add_contact

  def get_contacts(self,  desired_class=gdata_contacts_data.ContactsFeed,
                   auth_token=None, **kwargs):
    """Obtains a feed with the contacts belonging to the current user.
    
    Args:
      auth_token: An object which sets the Authorization HTTP header in its
                  modify_request method. Recommended classes include
                  gdata_gauth.ClientLoginToken and gdata_gauth.AuthSubToken
                  among others. Represents the current user. Defaults to None
                  and if None, this method will look for a value in the
                  auth_token member of SpreadsheetsClient.
      desired_class: class descended from atom.core.XmlElement to which a
                     successful response should be converted. If there is no
                     converter function specified (desired_class=None) then the
                     desired_class will be used in calling the
                     atom.core.parse function. If neither
                     the desired_class nor the converter is specified, an
                     HTTP reponse object will be returned. Defaults to
                     gdata.spreadsheets.data.SpreadsheetsFeed.
    """
    return self.get_feed(self.GetFeedUri(), auth_token=auth_token,
                         desired_class=desired_class, **kwargs)

  GetContacts = get_contacts

  def get_group(self, uri=None, desired_class=gdata_contacts_data.GroupEntry,
                auth_token=None, **kwargs):
    """ Get a single groups details 
    Args:
        uri:  the group uri or id   
    """
    return self.get(uri, desired_class=desired_class, auth_token=auth_token, **kwargs)

  GetGroup = get_group

  def get_groups(self, uri=None, desired_class=gdata_contacts_data.GroupsFeed,
                 auth_token=None, **kwargs):
    uri = uri or self.GetFeedUri('groups')
    return self.get_feed(uri, desired_class=desired_class, auth_token=auth_token, **kwargs)

  GetGroups = get_groups

  def create_group(self, new_group, insert_uri=None, url_params=None, 
                   desired_class=None):
    insert_uri = insert_uri or self.GetFeedUri('groups')
    return self.Post(new_group, insert_uri, url_params=url_params,
        desired_class=desired_class)

  CreateGroup = create_group

  def update_group(self, edit_uri, updated_group, url_params=None,
                   escape_params=True, desired_class=None):
    return self.Put(updated_group, self._CleanUri(edit_uri),
                    url_params=url_params,
                    escape_params=escape_params,
                    desired_class=desired_class)

  UpdateGroup = update_group

  def delete_group(self, edit_uri, extra_headers=None,
                   url_params=None, escape_params=True):
    return self.Delete(self._CleanUri(edit_uri),
                       url_params=url_params, escape_params=escape_params)

  DeleteGroup = delete_group

  def change_photo(self, media, contact_entry_or_url, content_type=None, 
                   content_length=None):
    """Change the photo for the contact by uploading a new photo.

    Performs a PUT against the photo edit URL to send the binary data for the
    photo.

    Args:
      media: filename, file-like-object, or a gdata.MediaSource object to send.
      contact_entry_or_url: ContactEntry or str If it is a ContactEntry, this
                            method will search for an edit photo link URL and
                            perform a PUT to the URL.
      content_type: str (optional) the mime type for the photo data. This is
                    necessary if media is a file or file name, but if media
                    is a MediaSource object then the media object can contain
                    the mime type. If media_type is set, it will override the
                    mime type in the media object.
      content_length: int or str (optional) Specifying the content length is
                      only required if media is a file-like object. If media
                      is a filename, the length is determined using
                      os.path.getsize. If media is a MediaSource object, it is
                      assumed that it already contains the content length.
    """
    if isinstance(contact_entry_or_url, gdata_contacts_data.ContactEntry):
      url = contact_entry_or_url.GetPhotoEditLink().href
    else:
      url = contact_entry_or_url
    if isinstance(media, gdata.MediaSource):
      payload = media
    # If the media object is a file-like object, then use it as the file
    # handle in the in the MediaSource.
    elif hasattr(media, 'read'):
      payload = gdata.MediaSource(file_handle=media, 
          content_type=content_type, content_length=content_length)
    # Assume that the media object is a file name.
    else:
      payload = gdata.MediaSource(content_type=content_type, 
          content_length=content_length, file_path=media)
    return self.Put(payload, url)

  ChangePhoto = change_photo

  def get_photo(self, contact_entry_or_url):
    """Retrives the binary data for the contact's profile photo as a string.
    
    Args:
      contact_entry_or_url: a gdata.contacts.ContactEntry objecr or a string
         containing the photo link's URL. If the contact entry does not 
         contain a photo link, the image will not be fetched and this method
         will return None.
    """
    # TODO: add the ability to write out the binary image data to a file, 
    # reading and writing a chunk at a time to avoid potentially using up 
    # large amounts of memory.
    url = None
    if isinstance(contact_entry_or_url, gdata_contacts_data.ContactEntry):
      photo_link = contact_entry_or_url.GetPhotoLink()
      if photo_link:
        url = photo_link.href
    else:
      url = contact_entry_or_url
    if url:
      return self.Get(url, desired_class=str)
    else:
      return None

  GetPhoto = get_photo

  def delete_photo(self, contact_entry_or_url):
    url = None
    if isinstance(contact_entry_or_url, gdata_contacts_data.ContactEntry):
      url = contact_entry_or_url.GetPhotoEditLink().href
    else:
      url = contact_entry_or_url
    if url:
      self.Delete(url)

  DeletePhoto = delete_photo

  def get_profiles_feed(self, uri=None):
    """Retrieves a feed containing all domain's profiles.

    Args:
      uri: string (optional) the URL to retrieve the profiles feed,
          for example /m8/feeds/profiles/default/full

    Returns:
      On success, a ProfilesFeed containing the profiles.
      On failure, raises a RequestError.
    """
    
    uri = uri or self.GetFeedUri('profiles')    
    return self.Get(uri,
                    desired_class=gdata_contacts_data.ProfilesFeedFromString)

  GetProfilesFeed = get_profiles_feed

  def get_profile(self, uri):
    """Retrieves a domain's profile for the user.

    Args:
      uri: string the URL to retrieve the profiles feed,
          for example /m8/feeds/profiles/default/full/username

    Returns:
      On success, a ProfileEntry containing the profile for the user.
      On failure, raises a RequestError
    """
    return self.Get(uri,
                    desired_class=gdata_contacts_data.ProfileEntryFromString)

  GetProfile = get_profile

  def update_profile(self, edit_uri, updated_profile,  auth_token=None,  **kwargs):
    """Updates an existing profile.

    Args:
      edit_uri: string The edit link URI for the element being updated
      updated_profile: string atom.Entry or subclass containing
                    the Atom Entry which will replace the profile which is
                    stored at the edit_url.
      url_params: dict (optional) Additional URL parameters to be included
                  in the update request.
      escape_params: boolean (optional) If true, the url_params will be
                     escaped before they are included in the request.

    Returns:
      On successful update,  a httplib.HTTPResponse containing the server's
        response to the PUT request.
      On failure, raises a RequestError.
    """
    return self.Put(updated_profile, self._CleanUri(edit_uri),
                    desired_class=gdata_contacts_data.ProfileEntryFromString)

  UpdateProfile = update_profile

  def execute_batch(self, batch_feed, url, desired_class=None):
    """Sends a batch request feed to the server.
    
    Args:
      batch_feed: gdata.contacts.ContactFeed A feed containing batch
          request entries. Each entry contains the operation to be performed
          on the data contained in the entry. For example an entry with an
          operation type of insert will be used as if the individual entry
          had been inserted.
      url: str The batch URL to which these operations should be applied.
      converter: Function (optional) The function used to convert the server's
          response to an object. 
    
    Returns:
      The results of the batch request's execution on the server. If the
      default converter is used, this is stored in a ContactsFeed.
    """
    return self.Post(batch_feed, url, desired_class=desired_class)

  ExecuteBatch = execute_batch

  def execute_batch_profiles(self, batch_feed, url,
                   desired_class=gdata_contacts_data.ProfilesFeedFromString):
    """Sends a batch request feed to the server.

    Args:
      batch_feed: gdata.profiles.ProfilesFeed A feed containing batch
          request entries. Each entry contains the operation to be performed
          on the data contained in the entry. For example an entry with an
          operation type of insert will be used as if the individual entry
          had been inserted.
      url: string The batch URL to which these operations should be applied.
      converter: Function (optional) The function used to convert the server's
          response to an object. The default value is
          gdata.profiles.ProfilesFeedFromString.

    Returns:
      The results of the batch request's execution on the server. If the
      default converter is used, this is stored in a ProfilesFeed.
    """
    return self.Post(batch_feed, url, desired_class=desired_class)

  ExecuteBatchProfiles = execute_batch_profiles


class ContactsQuery(gdata_client.Query):
  """ 
  Create a custom Contacts Query
  
  Full specs can be found at: U{Contacts query parameters reference
  <http://code.google.com/apis/contacts/docs/3.0/reference.html#Parameters>} 
  """
  
  def __init__(self, feed=None, group=None, orderby=None, showdeleted=None,
               sortorder=None, requirealldeleted=None, **kwargs):
    """ 
    @param max_results: The maximum number of entries to return. If you want 
        to receive all of the contacts, rather than only the default maximum, you 
        can specify a very large number for max-results.
    @param start-index: The 1-based index of the first result to be retrieved.
    @param updated-min: The lower bound on entry update dates.
    @param group: Constrains the results to only the contacts belonging to the
        group specified. Value of this parameter specifies group ID
    @param orderby:  Sorting criterion. The only supported value is 
        lastmodified.
    @param showdeleted: Include deleted contacts in the returned contacts feed
    @pram sortorder: Sorting order direction. Can be either ascending or
        descending.
    @param requirealldeleted: Only relevant if showdeleted and updated-min 
        are also provided. It dictates the behavior of the server in case it 
        detects that placeholders of some entries deleted since the point in
        time specified as updated-min may have been lost.
    """
    gdata_client.Query.__init__(self, **kwargs)
    self.group = group
    self.orderby = orderby
    self.sortorder = sortorder
    self.showdeleted = showdeleted

  def modify_request(self, http_request):
    if self.group:
      gdata_client._add_query_param('group', self.group, http_request)
    if self.orderby:
      gdata_client._add_query_param('orderby', self.orderby, http_request)
    if self.sortorder:
      gdata_client._add_query_param('sortorder', self.sortorder, http_request)
    if self.showdeleted:
      gdata_client._add_query_param('showdeleted', self.showdeleted, http_request)
    gdata_client.Query.modify_request(self, http_request)

  ModifyRequest = modify_request
    

class ProfilesQuery(gdata_client.Query):
  def __init__(self, feed=None):
    self.feed = feed or 'http://www.google.com/m8/feeds/profiles/default/full'
    

  def _CleanUri(self, uri):
    """Sanitizes a feed URI.

    Args:
      uri: The URI to sanitize, can be relative or absolute.

    Returns:
      The given URI without its http://server prefix, if any.
      Keeps the leading slash of the URI.
    """
    url_prefix = 'http://%s' % self.server
    if uri.startswith(url_prefix):
      uri = uri[len(url_prefix):]
    return uri                 

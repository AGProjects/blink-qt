#
# Copyright (C) 2006,2008 Google Inc.
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


"""GDataService provides CRUD ops. and programmatic login for GData services.

  Error: A base exception class for all exceptions in the gdata_client
         module.

  CaptchaRequired: This exception is thrown when a login attempt results in a
                   captcha challenge from the ClientLogin service. When this
                   exception is thrown, the captcha_token and captcha_url are
                   set to the values provided in the server's response.

  BadAuthentication: Raised when a login attempt is made with an incorrect
                     username or password.

  NotAuthenticated: Raised if an operation requiring authentication is called
                    before a user has authenticated.

  NonAuthSubToken: Raised if a method to modify an AuthSub token is used when
                   the user is either not authenticated or is authenticated
                   through another authentication mechanism.

  NonOAuthToken: Raised if a method to modify an OAuth token is used when the
                 user is either not authenticated or is authenticated through
                 another authentication mechanism.

  RequestError: Raised if a CRUD request returned a non-success code.

  UnexpectedReturnType: Raised if the response from the server was not of the
                        desired type. For example, this would be raised if the
                        server sent a feed when the client requested an entry.

  GDataService: Encapsulates user credentials needed to perform insert, update
                and delete operations with the GData API. An instance can
                perform user authentication, query, insertion, deletion, and 
                update.

  Query: Eases query URI creation by allowing URI parameters to be set as 
         dictionary attributes. For example a query with a feed of 
         '/base/feeds/snippets' and ['bq'] set to 'digital camera' will 
         produce '/base/feeds/snippets?bq=digital+camera' when .ToUri() is 
         called on it.
"""


__author__ = 'api.jscudder (Jeffrey Scudder)'

import re
import urllib

from blink.google import atom
from blink.google.atom import http_interface as atom_http_interface, service as atom_service, token_store as atom_token_store
from blink.google import gdata
from blink.google.gdata import gauth as gdata_gauth

try:
  from xml.etree import cElementTree as ElementTree
except ImportError:
  try:
    import cElementTree as ElementTree
  except ImportError:
    try:
      from xml.etree import ElementTree
    except ImportError:
      from elementtree import ElementTree

AUTH_SERVER_HOST = 'https://www.google.com'


# Maps the service names used in ClientLogin to scope URLs.
CLIENT_LOGIN_SCOPES = gdata_gauth.AUTH_SCOPES
# Default parameters for GDataService.GetWithRetries method
DEFAULT_NUM_RETRIES = 3
DEFAULT_DELAY = 1
DEFAULT_BACKOFF = 2


def lookup_scopes(service_name):
  """Finds the scope URLs for the desired service.

  In some cases, an unknown service may be used, and in those cases this
  function will return None.
  """
  if service_name in CLIENT_LOGIN_SCOPES:
    return CLIENT_LOGIN_SCOPES[service_name]
  return None


# Module level variable specifies which module should be used by GDataService
# objects to make HttpRequests. This setting can be overridden on each 
# instance of GDataService.
# This module level variable is deprecated. Reassign the http_client member
# of a GDataService object instead.
http_request_handler = atom_service


class Error(Exception):
  pass


class CaptchaRequired(Error):
  pass


class BadAuthentication(Error):
  pass


class NotAuthenticated(Error):
  pass


class NonAuthSubToken(Error):
  pass


class NonOAuthToken(Error):
  pass


class RequestError(Error):
  pass


class UnexpectedReturnType(Error):
  pass


class BadAuthenticationServiceURL(Error):
  pass


class FetchingOAuthRequestTokenFailed(RequestError):
  pass


class TokenUpgradeFailed(RequestError):
  pass


class RevokingOAuthTokenFailed(RequestError):
  pass


class AuthorizationRequired(Error):
  pass


class TokenHadNoScope(Error):
  pass


class RanOutOfTries(Error):
  pass


class GDataService(atom_service.AtomService):
  """Contains elements needed for GData login and CRUD request headers.

  Maintains additional headers (tokens for example) needed for the GData 
  services to allow a user to perform inserts, updates, and deletes.
  """
  # The hander member is deprecated, use http_client instead.
  handler = None
  # The auth_token member is deprecated, use the token_store instead.
  auth_token = None
  # The tokens dict is deprecated in favor of the token_store.
  tokens = None

  def __init__(self, email=None, password=None, account_type='HOSTED_OR_GOOGLE',
               service=None, auth_service_url=None, source=None, server=None, 
               additional_headers=None, handler=None, tokens=None,
               http_client=None, token_store=None):
    """Creates an object of type GDataService.

    Args:
      email: string (optional) The user's email address, used for
          authentication.
      password: string (optional) The user's password.
      account_type: string (optional) The type of account to use. Use
          'GOOGLE' for regular Google accounts or 'HOSTED' for Google
          Apps accounts, or 'HOSTED_OR_GOOGLE' to try finding a HOSTED
          account first and, if it doesn't exist, try finding a regular
          GOOGLE account. Default value: 'HOSTED_OR_GOOGLE'.
      service: string (optional) The desired service for which credentials
          will be obtained.
      auth_service_url: string (optional) User-defined auth token request URL
          allows users to explicitly specify where to send auth token requests.
      source: string (optional) The name of the user's application.
      server: string (optional) The name of the server to which a connection
          will be opened. Default value: 'base.google.com'.
      additional_headers: dictionary (optional) Any additional headers which 
          should be included with CRUD operations.
      handler: module (optional) This parameter is deprecated and has been
          replaced by http_client.
      tokens: This parameter is deprecated, calls should be made to 
          token_store instead.
      http_client: An object responsible for making HTTP requests using a
          request method. If none is provided, a new instance of
          atom.http.ProxiedHttpClient will be used.
      token_store: Keeps a collection of authorization tokens which can be
          applied to requests for a specific URLs. Critical methods are
          find_token based on a URL (atom.url.Url or a string), add_token,
          and remove_token.
    """
    atom_service.AtomService.__init__(self, http_client=http_client, 
        token_store=token_store)
    self.email = email
    self.password = password
    self.account_type = account_type
    self.service = service
    self.auth_service_url = auth_service_url
    self.server = server
    self.additional_headers = additional_headers or {}
    self.__SetSource(source)
    self.__captcha_token = None
    self.__captcha_url = None
    self.__gsessionid = None

    if http_request_handler.__name__ == 'gdata.urlfetch':
      import gdata.alt.appengine
      self.http_client = gdata.alt.appengine.AppEngineHttpClient()

  def _SetSessionId(self, session_id):
    """Used in unit tests to simulate a 302 which sets a gsessionid."""
    self.__gsessionid = session_id
 
  def _GetAuthToken(self):
    """Returns the auth token used for authenticating requests.

    Returns:
      string
    """
    current_scopes = lookup_scopes(self.service)
    if current_scopes:
      token = self.token_store.find_token(current_scopes[0])
      if hasattr(token, 'auth_header'):
        return token.auth_header
    return None

  def GetGeneratorFromLinkFinder(self, link_finder, func, 
                                 num_retries=DEFAULT_NUM_RETRIES,
                                 delay=DEFAULT_DELAY,
                                 backoff=DEFAULT_BACKOFF):
    """returns a generator for pagination"""
    yield link_finder
    next = link_finder.GetNextLink()
    while next is not None:
      next_feed = func(str(self.GetWithRetries(
            next.href, num_retries=num_retries, delay=delay, backoff=backoff)))
      yield next_feed
      next = next_feed.GetNextLink()

  def _GetElementGeneratorFromLinkFinder(self, link_finder, func,
                                        num_retries=DEFAULT_NUM_RETRIES,
                                        delay=DEFAULT_DELAY,
                                        backoff=DEFAULT_BACKOFF):
    for element in self.GetGeneratorFromLinkFinder(link_finder, func,
                                                   num_retries=num_retries,
                                                   delay=delay,
                                                   backoff=backoff).entry:
      yield element

  def GetClientLoginToken(self):
    """Returns the token string for the current token or a token matching the 
    service scope.

    If the current_token is a ClientLoginToken, the token string for 
    the current token is returned. If the current_token is not set, this method
    searches for a token in the token_store which is valid for the service 
    object's current scope.

    The current scope is determined by the service name string member.
    The token string is the end of the Authorization header, it doesn not
    include the ClientLogin label.
    """
    if isinstance(self.current_token, gdata.auth.ClientLoginToken):
      return self.current_token.get_token_string()
    current_scopes = lookup_scopes(self.service)
    if current_scopes:
      token = self.token_store.find_token(current_scopes[0])
      if isinstance(token, gdata.auth.ClientLoginToken):
        return token.get_token_string()
    else:
      token = self.token_store.find_token(atom_token_store.SCOPE_ALL)
      if isinstance(token, gdata.auth.ClientLoginToken):
        return token.get_token_string()
      return None

  def SetClientLoginToken(self, token, scopes=None):
    """Sets the token sent in requests to a ClientLogin token.

    This method sets the current_token to a new ClientLoginToken and it 
    also attempts to add the ClientLoginToken to the token_store.
    
    Only use this method if you have received a token from the ClientLogin
    service. The auth_token is set automatically when ProgrammaticLogin()
    is used. See documentation for Google ClientLogin here:
    http://code.google.com/apis/accounts/docs/AuthForInstalledApps.html

    Args:
      token: string or instance of a ClientLoginToken. 
    """
    if not isinstance(token, gdata.auth.ClientLoginToken):
      token_string = token
      token = gdata.auth.ClientLoginToken()
      token.set_token_string(token_string)

    if not token.scopes:
      if scopes is None:
        scopes = lookup_scopes(self.service)
        if scopes is None:
          scopes = [atom_token_store.SCOPE_ALL]
      token.scopes = scopes
    if self.auto_set_current_token:
      self.current_token = token
    if self.auto_store_tokens:
      self.token_store.add_token(token)

  # Private methods to create the source property.
  def __GetSource(self):
    return self.__source

  def __SetSource(self, new_source):
    self.__source = new_source
    # Update the UserAgent header to include the new application name.
    self.additional_headers['User-Agent'] = atom_http_interface.USER_AGENT % (
        self.__source,)

  source = property(__GetSource, __SetSource, 
      doc="""The source is the name of the application making the request. 
             It should be in the form company_id-app_name-app_version""")

  # Authentication operations

  def ProgrammaticLogin(self, captcha_token=None, captcha_response=None):
    """Authenticates the user and sets the GData Auth token.

    Login retreives a temporary auth token which must be used with all
    requests to GData services. The auth token is stored in the GData client
    object.

    Login is also used to respond to a captcha challenge. If the user's login
    attempt failed with a CaptchaRequired error, the user can respond by
    calling Login with the captcha token and the answer to the challenge.

    Args:
      captcha_token: string (optional) The identifier for the captcha challenge
                     which was presented to the user.
      captcha_response: string (optional) The user's answer to the captch 
                        challenge.

    Raises:
      CaptchaRequired if the login service will require a captcha response
      BadAuthentication if the login service rejected the username or password
      Error if the login service responded with a 403 different from the above
    """
    request_body = gdata.auth.generate_client_login_request_body(self.email,
        self.password, self.service, self.source, self.account_type,
        captcha_token, captcha_response)

    # If the user has defined their own authentication service URL, 
    # send the ClientLogin requests to this URL:
    if not self.auth_service_url:
        auth_request_url = AUTH_SERVER_HOST + '/accounts/ClientLogin' 
    else:
        auth_request_url = self.auth_service_url

    auth_response = self.http_client.request('POST', auth_request_url,
        data=request_body, 
        headers={'Content-Type':'application/x-www-form-urlencoded'})
    response_body = auth_response.read()

    if auth_response.status == 200:
      # TODO: insert the token into the token_store directly.
      self.SetClientLoginToken(
          gdata.auth.get_client_login_token(response_body))
      self.__captcha_token = None
      self.__captcha_url = None

    elif auth_response.status == 403:
      # Examine each line to find the error type and the captcha token and
      # captch URL if they are present.
      captcha_parameters = gdata.auth.get_captcha_challenge(response_body,
          captcha_base_url='%s/accounts/' % AUTH_SERVER_HOST)
      if captcha_parameters:
        self.__captcha_token = captcha_parameters['token']
        self.__captcha_url = captcha_parameters['url']
        raise CaptchaRequired, 'Captcha Required'
      elif response_body.splitlines()[0] == 'Error=BadAuthentication':
        self.__captcha_token = None
        self.__captcha_url = None
        raise BadAuthentication, 'Incorrect username or password'
      else:
        self.__captcha_token = None
        self.__captcha_url = None
        raise Error, 'Server responded with a 403 code'
    elif auth_response.status == 302:
      self.__captcha_token = None
      self.__captcha_url = None
      # Google tries to redirect all bad URLs back to 
      # http://www.google.<locale>. If a redirect
      # attempt is made, assume the user has supplied an incorrect authentication URL
      raise BadAuthenticationServiceURL, 'Server responded with a 302 code.'

  def ClientLogin(self, username, password, account_type=None, service=None,
      auth_service_url=None, source=None, captcha_token=None, 
      captcha_response=None):
    """Convenience method for authenticating using ProgrammaticLogin. 
    
    Sets values for email, password, and other optional members.

    Args:
      username:
      password:
      account_type: string (optional)
      service: string (optional)
      auth_service_url: string (optional)
      captcha_token: string (optional)
      captcha_response: string (optional)
    """
    self.email = username
    self.password = password

    if account_type:
      self.account_type = account_type
    if service:
      self.service = service
    if source:
      self.source = source
    if auth_service_url:
      self.auth_service_url = auth_service_url

    self.ProgrammaticLogin(captcha_token, captcha_response)

  def GetWithRetries(self, uri, extra_headers=None, redirects_remaining=4, 
      encoding='UTF-8', converter=None, num_retries=DEFAULT_NUM_RETRIES,
      delay=DEFAULT_DELAY, backoff=DEFAULT_BACKOFF, logger=None):
    """This is a wrapper method for Get with retrying capability.

    To avoid various errors while retrieving bulk entities by retrying
    specified times.

    Note this method relies on the time module and so may not be usable
    by default in Python2.2.

    Args:
      num_retries: Integer; the retry count.
      delay: Integer; the initial delay for retrying.
      backoff: Integer; how much the delay should lengthen after each failure.
      logger: An object which has a debug(str) method to receive logging
              messages. Recommended that you pass in the logging module.
    Raises:
      ValueError if any of the parameters has an invalid value.
      RanOutOfTries on failure after number of retries.
    """
    # Moved import for time module inside this method since time is not a
    # default module in Python2.2. This method will not be usable in
    # Python2.2.
    import time
    if backoff <= 1:
      raise ValueError("backoff must be greater than 1")
    num_retries = int(num_retries)

    if num_retries < 0:
      raise ValueError("num_retries must be 0 or greater")

    if delay <= 0:
      raise ValueError("delay must be greater than 0")

    # Let's start
    mtries, mdelay = num_retries, delay
    while mtries > 0:
      if mtries != num_retries:
        if logger:
          logger.debug("Retrying: %s" % uri)
      try:
        rv = self.Get(uri, extra_headers=extra_headers,
                      redirects_remaining=redirects_remaining,
                      encoding=encoding, converter=converter)
      except SystemExit:
        # Allow this error
        raise
      except RequestError, e:
        # Error 500 is 'internal server error' and warrants a retry
        # Error 503 is 'service unavailable' and warrants a retry
        if e[0]['status'] not in [500, 503]:
          raise e
        # Else, fall through to the retry code...
      except Exception, e:
        if logger:
          logger.debug(e)
        # Fall through to the retry code...
      else:
        # This is the right path.
        return rv
      mtries -= 1
      time.sleep(mdelay)
      mdelay *= backoff
    raise RanOutOfTries('Ran out of tries.')

  # CRUD operations
  def Get(self, uri, extra_headers=None, redirects_remaining=4, 
      encoding='UTF-8', converter=None):
    """Query the GData API with the given URI

    The uri is the portion of the URI after the server value 
    (ex: www.google.com).

    To perform a query against Google Base, set the server to 
    'base.google.com' and set the uri to '/base/feeds/...', where ... is 
    your query. For example, to find snippets for all digital cameras uri 
    should be set to: '/base/feeds/snippets?bq=digital+camera'

    Args:
      uri: string The query in the form of a URI. Example:
           '/base/feeds/snippets?bq=digital+camera'.
      extra_headers: dictionary (optional) Extra HTTP headers to be included
                     in the GET request. These headers are in addition to 
                     those stored in the client's additional_headers property.
                     The client automatically sets the Content-Type and 
                     Authorization headers.
      redirects_remaining: int (optional) Tracks the number of additional
          redirects this method will allow. If the service object receives
          a redirect and remaining is 0, it will not follow the redirect. 
          This was added to avoid infinite redirect loops.
      encoding: string (optional) The character encoding for the server's
          response. Default is UTF-8
      converter: func (optional) A function which will transform
          the server's results before it is returned. Example: use 
          GDataFeedFromString to parse the server response as if it
          were a GDataFeed.

    Returns:
      If there is no ResultsTransformer specified in the call, a GDataFeed 
      or GDataEntry depending on which is sent from the server. If the 
      response is niether a feed or entry and there is no ResultsTransformer,
      return a string. If there is a ResultsTransformer, the returned value 
      will be that of the ResultsTransformer function.
    """

    if extra_headers is None:
      extra_headers = {}

    if self.__gsessionid is not None:
      if uri.find('gsessionid=') < 0:
        if uri.find('?') > -1:
          uri += '&gsessionid=%s' % (self.__gsessionid,)
        else:
          uri += '?gsessionid=%s' % (self.__gsessionid,)

    server_response = self.request('GET', uri, 
        headers=extra_headers)
    result_body = server_response.read()

    if server_response.status == 200:
      if converter:
        return converter(result_body)
      # There was no ResultsTransformer specified, so try to convert the
      # server's response into a GDataFeed.
      feed = gdata.GDataFeedFromString(result_body)
      if not feed:
        # If conversion to a GDataFeed failed, try to convert the server's
        # response to a GDataEntry.
        entry = gdata.GDataEntryFromString(result_body)
        if not entry:
          # The server's response wasn't a feed, or an entry, so return the
          # response body as a string.
          return result_body
        return entry
      return feed
    elif server_response.status == 302:
      if redirects_remaining > 0:
        location = (server_response.getheader('Location')
                    or server_response.getheader('location'))
        if location is not None:
          m = re.compile('[\?\&]gsessionid=(\w*)').search(location)
          if m is not None:
            self.__gsessionid = m.group(1)
          return GDataService.Get(self, location, extra_headers, redirects_remaining - 1, 
              encoding=encoding, converter=converter)
        else:
          raise RequestError, {'status': server_response.status,
              'reason': '302 received without Location header',
              'body': result_body}
      else:
        raise RequestError, {'status': server_response.status,
            'reason': 'Redirect received, but redirects_remaining <= 0',
            'body': result_body}
    else:
      raise RequestError, {'status': server_response.status,
          'reason': server_response.reason, 'body': result_body}

  def GetMedia(self, uri, extra_headers=None):
    """Returns a MediaSource containing media and its metadata from the given
    URI string.
    """
    response_handle = self.request('GET', uri,
        headers=extra_headers)
    return gdata.MediaSource(response_handle, response_handle.getheader(
            'Content-Type'),
        response_handle.getheader('Content-Length'))

  def GetEntry(self, uri, extra_headers=None):
    """Query the GData API with the given URI and receive an Entry.
    
    See also documentation for gdata.service.Get

    Args:
      uri: string The query in the form of a URI. Example:
           '/base/feeds/snippets?bq=digital+camera'.
      extra_headers: dictionary (optional) Extra HTTP headers to be included
                     in the GET request. These headers are in addition to
                     those stored in the client's additional_headers property.
                     The client automatically sets the Content-Type and
                     Authorization headers.

    Returns:
      A GDataEntry built from the XML in the server's response.
    """

    result = GDataService.Get(self, uri, extra_headers, 
        converter=atom.EntryFromString)
    if isinstance(result, atom.Entry):
      return result
    else:
      raise UnexpectedReturnType, 'Server did not send an entry' 

  def GetFeed(self, uri, extra_headers=None, 
              converter=gdata.GDataFeedFromString):
    """Query the GData API with the given URI and receive a Feed.

    See also documentation for gdata.service.Get

    Args:
      uri: string The query in the form of a URI. Example:
           '/base/feeds/snippets?bq=digital+camera'.
      extra_headers: dictionary (optional) Extra HTTP headers to be included
                     in the GET request. These headers are in addition to
                     those stored in the client's additional_headers property.
                     The client automatically sets the Content-Type and
                     Authorization headers.

    Returns:
      A GDataFeed built from the XML in the server's response.
    """

    result = GDataService.Get(self, uri, extra_headers, converter=converter)
    if isinstance(result, atom.Feed):
      return result
    else:
      raise UnexpectedReturnType, 'Server did not send a feed'  

  def GetNext(self, feed):
    """Requests the next 'page' of results in the feed.
    
    This method uses the feed's next link to request an additional feed
    and uses the class of the feed to convert the results of the GET request.

    Args:
      feed: atom.Feed or a subclass. The feed should contain a next link and
          the type of the feed will be applied to the results from the 
          server. The new feed which is returned will be of the same class
          as this feed which was passed in.

    Returns:
      A new feed representing the next set of results in the server's feed.
      The type of this feed will match that of the feed argument.
    """
    next_link = feed.GetNextLink()
    # Create a closure which will convert an XML string to the class of
    # the feed object passed in.
    def ConvertToFeedClass(xml_string):
      return atom.CreateClassFromXMLString(feed.__class__, xml_string)
    # Make a GET request on the next link and use the above closure for the
    # converted which processes the XML string from the server.
    if next_link and next_link.href:
      return GDataService.Get(self, next_link.href, 
          converter=ConvertToFeedClass)
    else:
      return None

  def Post(self, data, uri, extra_headers=None, url_params=None,
           escape_params=True, redirects_remaining=4, media_source=None,
           converter=None):
    """Insert or update  data into a GData service at the given URI.

    Args:
      data: string, ElementTree._Element, atom.Entry, or gdata.GDataEntry The
            XML to be sent to the uri.
      uri: string The location (feed) to which the data should be inserted.
           Example: '/base/feeds/items'.
      extra_headers: dict (optional) HTTP headers which are to be included.
                     The client automatically sets the Content-Type,
                     Authorization, and Content-Length headers.
      url_params: dict (optional) Additional URL parameters to be included
                  in the URI. These are translated into query arguments
                  in the form '&dict_key=value&...'.
                  Example: {'max-results': '250'} becomes &max-results=250
      escape_params: boolean (optional) If false, the calling code has already
                     ensured that the query will form a valid URL (all
                     reserved characters have been escaped). If true, this
                     method will escape the query and any URL parameters
                     provided.
      media_source: MediaSource (optional) Container for the media to be sent
          along with the entry, if provided.
      converter: func (optional) A function which will be executed on the
          server's response. Often this is a function like
          GDataEntryFromString which will parse the body of the server's
          response and return a GDataEntry.

    Returns:
      If the post succeeded, this method will return a GDataFeed, GDataEntry,
      or the results of running converter on the server's result body (if
      converter was specified).
    """
    return GDataService.PostOrPut(self, 'POST', data, uri, 
        extra_headers=extra_headers, url_params=url_params, 
        escape_params=escape_params, redirects_remaining=redirects_remaining,
        media_source=media_source, converter=converter)

  def PostOrPut(self, verb, data, uri, extra_headers=None, url_params=None, 
           escape_params=True, redirects_remaining=4, media_source=None, 
           converter=None):
    """Insert data into a GData service at the given URI.

    Args:
      verb: string, either 'POST' or 'PUT'
      data: string, ElementTree._Element, atom.Entry, or gdata.GDataEntry The
            XML to be sent to the uri. 
      uri: string The location (feed) to which the data should be inserted. 
           Example: '/base/feeds/items'. 
      extra_headers: dict (optional) HTTP headers which are to be included. 
                     The client automatically sets the Content-Type,
                     Authorization, and Content-Length headers.
      url_params: dict (optional) Additional URL parameters to be included
                  in the URI. These are translated into query arguments
                  in the form '&dict_key=value&...'.
                  Example: {'max-results': '250'} becomes &max-results=250
      escape_params: boolean (optional) If false, the calling code has already
                     ensured that the query will form a valid URL (all
                     reserved characters have been escaped). If true, this
                     method will escape the query and any URL parameters
                     provided.
      media_source: MediaSource (optional) Container for the media to be sent
          along with the entry, if provided.
      converter: func (optional) A function which will be executed on the 
          server's response. Often this is a function like 
          GDataEntryFromString which will parse the body of the server's 
          response and return a GDataEntry.

    Returns:
      If the post succeeded, this method will return a GDataFeed, GDataEntry,
      or the results of running converter on the server's result body (if
      converter was specified).
    """
    if extra_headers is None:
      extra_headers = {}

    if self.__gsessionid is not None:
      if uri.find('gsessionid=') < 0:
        if url_params is None:
          url_params = {}
        url_params['gsessionid'] = self.__gsessionid

    if data and media_source:
      if ElementTree.iselement(data):
        data_str = ElementTree.tostring(data)
      else:
        data_str = str(data)
        
      multipart = []
      multipart.append('Media multipart posting\r\n--END_OF_PART\r\n' + \
          'Content-Type: application/atom+xml\r\n\r\n')
      multipart.append('\r\n--END_OF_PART\r\nContent-Type: ' + \
          media_source.content_type+'\r\n\r\n')
      multipart.append('\r\n--END_OF_PART--\r\n')
        
      extra_headers['MIME-version'] = '1.0'
      extra_headers['Content-Length'] = str(len(multipart[0]) +
          len(multipart[1]) + len(multipart[2]) +
          len(data_str) + media_source.content_length)

      extra_headers['Content-Type'] = 'multipart/related; boundary=END_OF_PART'
      server_response = self.request(verb, uri, 
          data=[multipart[0], data_str, multipart[1], media_source.file_handle,
              multipart[2]], headers=extra_headers, url_params=url_params)
      result_body = server_response.read()
      
    elif media_source or isinstance(data, gdata.MediaSource):
      if isinstance(data, gdata.MediaSource):
        media_source = data
      extra_headers['Content-Length'] = str(media_source.content_length)
      extra_headers['Content-Type'] = media_source.content_type
      server_response = self.request(verb, uri, 
          data=media_source.file_handle, headers=extra_headers,
          url_params=url_params)
      result_body = server_response.read()

    else:
      http_data = data
      content_type = 'application/atom+xml'
      extra_headers['Content-Type'] = content_type
      server_response = self.request(verb, uri, data=http_data,
          headers=extra_headers, url_params=url_params)
      result_body = server_response.read()

    # Server returns 201 for most post requests, but when performing a batch
    # request the server responds with a 200 on success.
    if server_response.status == 201 or server_response.status == 200:
      if converter:
        return converter(result_body)
      feed = gdata.GDataFeedFromString(result_body)
      if not feed:
        entry = gdata.GDataEntryFromString(result_body)
        if not entry:
          return result_body
        return entry
      return feed
    elif server_response.status == 302:
      if redirects_remaining > 0:
        location = (server_response.getheader('Location')
                    or server_response.getheader('location'))
        if location is not None:
          m = re.compile('[\?\&]gsessionid=(\w*)').search(location)
          if m is not None:
            self.__gsessionid = m.group(1) 
          return GDataService.PostOrPut(self, verb, data, location, 
              extra_headers, url_params, escape_params, 
              redirects_remaining - 1, media_source, converter=converter)
        else:
          raise RequestError, {'status': server_response.status,
              'reason': '302 received without Location header',
              'body': result_body}
      else:
        raise RequestError, {'status': server_response.status,
            'reason': 'Redirect received, but redirects_remaining <= 0',
            'body': result_body}
    else:
      raise RequestError, {'status': server_response.status,
          'reason': server_response.reason, 'body': result_body}

  def Put(self, data, uri, extra_headers=None, url_params=None, 
          escape_params=True, redirects_remaining=3, media_source=None,
          converter=None):
    """Updates an entry at the given URI.
     
    Args:
      data: string, ElementTree._Element, or xml_wrapper.ElementWrapper The 
            XML containing the updated data.
      uri: string A URI indicating entry to which the update will be applied.
           Example: '/base/feeds/items/ITEM-ID'
      extra_headers: dict (optional) HTTP headers which are to be included.
                     The client automatically sets the Content-Type,
                     Authorization, and Content-Length headers.
      url_params: dict (optional) Additional URL parameters to be included
                  in the URI. These are translated into query arguments
                  in the form '&dict_key=value&...'.
                  Example: {'max-results': '250'} becomes &max-results=250
      escape_params: boolean (optional) If false, the calling code has already
                     ensured that the query will form a valid URL (all
                     reserved characters have been escaped). If true, this
                     method will escape the query and any URL parameters
                     provided.
      converter: func (optional) A function which will be executed on the 
          server's response. Often this is a function like 
          GDataEntryFromString which will parse the body of the server's 
          response and return a GDataEntry.

    Returns:
      If the put succeeded, this method will return a GDataFeed, GDataEntry,
      or the results of running converter on the server's result body (if
      converter was specified).
    """
    return GDataService.PostOrPut(self, 'PUT', data, uri, 
        extra_headers=extra_headers, url_params=url_params, 
        escape_params=escape_params, redirects_remaining=redirects_remaining,
        media_source=media_source, converter=converter)

  def Delete(self, uri, extra_headers=None, url_params=None, 
             escape_params=True, redirects_remaining=4):
    """Deletes the entry at the given URI.

    Args:
      uri: string The URI of the entry to be deleted. Example: 
           '/base/feeds/items/ITEM-ID'
      extra_headers: dict (optional) HTTP headers which are to be included.
                     The client automatically sets the Content-Type and
                     Authorization headers.
      url_params: dict (optional) Additional URL parameters to be included
                  in the URI. These are translated into query arguments
                  in the form '&dict_key=value&...'.
                  Example: {'max-results': '250'} becomes &max-results=250
      escape_params: boolean (optional) If false, the calling code has already
                     ensured that the query will form a valid URL (all
                     reserved characters have been escaped). If true, this
                     method will escape the query and any URL parameters
                     provided.

    Returns:
      True if the entry was deleted.
    """
    if extra_headers is None:
      extra_headers = {}

    if self.__gsessionid is not None:
      if uri.find('gsessionid=') < 0:
        if url_params is None:
          url_params = {}
        url_params['gsessionid'] = self.__gsessionid
 
    server_response = self.request('DELETE', uri, 
        headers=extra_headers, url_params=url_params)
    result_body = server_response.read()

    if server_response.status == 200:
      return True
    elif server_response.status == 302:
      if redirects_remaining > 0:
        location = (server_response.getheader('Location')
                    or server_response.getheader('location'))
        if location is not None:
          m = re.compile('[\?\&]gsessionid=(\w*)').search(location)
          if m is not None:
            self.__gsessionid = m.group(1) 
          return GDataService.Delete(self, location, extra_headers, 
              url_params, escape_params, redirects_remaining - 1)
        else:
          raise RequestError, {'status': server_response.status,
              'reason': '302 received without Location header',
              'body': result_body}
      else:
        raise RequestError, {'status': server_response.status,
            'reason': 'Redirect received, but redirects_remaining <= 0',
            'body': result_body}
    else:
      raise RequestError, {'status': server_response.status,
          'reason': server_response.reason, 'body': result_body}


class Query(dict):
  """Constructs a query URL to be used in GET requests
  
  Url parameters are created by adding key-value pairs to this object as a 
  dict. For example, to add &max-results=25 to the URL do
  my_query['max-results'] = 25

  Category queries are created by adding category strings to the categories
  member. All items in the categories list will be concatenated with the /
  symbol (symbolizing a category x AND y restriction). If you would like to OR
  2 categories, append them as one string with a | between the categories. 
  For example, do query.categories.append('Fritz|Laurie') to create a query
  like this feed/-/Fritz%7CLaurie . This query will look for results in both
  categories.
  """

  def __init__(self, feed=None, text_query=None, params=None, 
      categories=None):
    """Constructor for Query
    
    Args:
      feed: str (optional) The path for the feed (Examples: 
          '/base/feeds/snippets' or 'calendar/feeds/jo@gmail.com/private/full'
      text_query: str (optional) The contents of the q query parameter. The
          contents of the text_query are URL escaped upon conversion to a URI.
      params: dict (optional) Parameter value string pairs which become URL
          params when translated to a URI. These parameters are added to the
          query's items (key-value pairs).
      categories: list (optional) List of category strings which should be
          included as query categories. See 
          http://code.google.com/apis/gdata/reference.html#Queries for 
          details. If you want to get results from category A or B (both 
          categories), specify a single list item 'A|B'. 
    """
    
    self.feed = feed
    self.categories = []
    if text_query:
      self.text_query = text_query
    if isinstance(params, dict):
      for param in params:
        self[param] = params[param]
    if isinstance(categories, list):
      for category in categories:
        self.categories.append(category)

  def _GetTextQuery(self):
    if 'q' in self.keys():
      return self['q']
    else:
      return None

  def _SetTextQuery(self, query):
    self['q'] = query

  text_query = property(_GetTextQuery, _SetTextQuery, 
      doc="""The feed query's q parameter""")

  def _GetAuthor(self):
    if 'author' in self.keys():
      return self['author']
    else:
      return None

  def _SetAuthor(self, query):
    self['author'] = query

  author = property(_GetAuthor, _SetAuthor,
      doc="""The feed query's author parameter""")

  def _GetAlt(self):
    if 'alt' in self.keys():
      return self['alt']
    else:
      return None

  def _SetAlt(self, query):
    self['alt'] = query

  alt = property(_GetAlt, _SetAlt,
      doc="""The feed query's alt parameter""")

  def _GetUpdatedMin(self):
    if 'updated-min' in self.keys():
      return self['updated-min']
    else:
      return None

  def _SetUpdatedMin(self, query):
    self['updated-min'] = query

  updated_min = property(_GetUpdatedMin, _SetUpdatedMin,
      doc="""The feed query's updated-min parameter""")

  def _GetUpdatedMax(self):
    if 'updated-max' in self.keys():
      return self['updated-max']
    else:
      return None

  def _SetUpdatedMax(self, query):
    self['updated-max'] = query

  updated_max = property(_GetUpdatedMax, _SetUpdatedMax,
      doc="""The feed query's updated-max parameter""")

  def _GetPublishedMin(self):
    if 'published-min' in self.keys():
      return self['published-min']
    else:
      return None

  def _SetPublishedMin(self, query):
    self['published-min'] = query

  published_min = property(_GetPublishedMin, _SetPublishedMin,
      doc="""The feed query's published-min parameter""")

  def _GetPublishedMax(self):
    if 'published-max' in self.keys():
      return self['published-max']
    else:
      return None

  def _SetPublishedMax(self, query):
    self['published-max'] = query

  published_max = property(_GetPublishedMax, _SetPublishedMax,
      doc="""The feed query's published-max parameter""")

  def _GetStartIndex(self):
    if 'start-index' in self.keys():
      return self['start-index']
    else:
      return None

  def _SetStartIndex(self, query):
    if not isinstance(query, str):
      query = str(query)
    self['start-index'] = query

  start_index = property(_GetStartIndex, _SetStartIndex,
      doc="""The feed query's start-index parameter""")

  def _GetMaxResults(self):
    if 'max-results' in self.keys():
      return self['max-results']
    else:
      return None

  def _SetMaxResults(self, query):
    if not isinstance(query, str):
      query = str(query)
    self['max-results'] = query

  max_results = property(_GetMaxResults, _SetMaxResults,
      doc="""The feed query's max-results parameter""")

  def _GetOrderBy(self):
    if 'orderby' in self.keys():
      return self['orderby']
    else:
      return None
 
  def _SetOrderBy(self, query):
    self['orderby'] = query
  
  orderby = property(_GetOrderBy, _SetOrderBy, 
      doc="""The feed query's orderby parameter""")

  def ToUri(self):
    q_feed = self.feed or ''
    category_string = '/'.join(
        [urllib.quote_plus(c) for c in self.categories])
    # Add categories to the feed if there are any.
    if len(self.categories) > 0:
      q_feed = q_feed + '/-/' + category_string
    return atom_service.BuildUri(q_feed, self)

  def __str__(self):
    return self.ToUri()

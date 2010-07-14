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


# This module is used for version 2 of the Google Data APIs.


"""Provides auth related token classes and functions for Google Data APIs.

Token classes represent a user's authorization of this app to access their
data. Usually these are not created directly but by a GDClient object.

ClientLoginToken
AuthSubToken
SecureAuthSubToken
OAuthHmacToken
OAuthRsaToken
TwoLeggedOAuthHmacToken
TwoLeggedOAuthRsaToken

Functions which are often used in application code (as opposed to just within
the gdata-python-client library) are the following:

generate_auth_sub_url
authorize_request_token

"""


__author__ = 'j.s@google.com (Jeff Scudder)'

import urllib


PROGRAMMATIC_AUTH_LABEL = 'GoogleLogin auth='
AUTHSUB_AUTH_LABEL = 'AuthSub token='


# This dict provides the AuthSub and OAuth scopes for all services by service
# name. The service name (key) is used in ClientLogin requests.
AUTH_SCOPES = {
    'cp': ( # Contacts API
        'https://www.google.com/m8/feeds/',
        'http://www.google.com/m8/feeds/')}



class Error(Exception):
  pass


class UnsupportedTokenType(Error):
  """Raised when token to or from blob is unable to convert the token."""
  pass


# ClientLogin functions and classes.
def generate_client_login_request_body(email, password, service, source,
    account_type='HOSTED_OR_GOOGLE', captcha_token=None,
    captcha_response=None):
  """Creates the body of the autentication request

  See http://code.google.com/apis/accounts/AuthForInstalledApps.html#Request
  for more details.

  Args:
    email: str
    password: str
    service: str
    source: str
    account_type: str (optional) Defaul is 'HOSTED_OR_GOOGLE', other valid
        values are 'GOOGLE' and 'HOSTED'
    captcha_token: str (optional)
    captcha_response: str (optional)

  Returns:
    The HTTP body to send in a request for a client login token.
  """
  # Create a POST body containing the user's credentials.
  request_fields = {'Email': email,
                    'Passwd': password,
                    'accountType': account_type,
                    'service': service,
                    'source': source}
  if captcha_token and captcha_response:
    # Send the captcha token and response as part of the POST body if the
    # user is responding to a captch challenge.
    request_fields['logintoken'] = captcha_token
    request_fields['logincaptcha'] = captcha_response
  return urllib.urlencode(request_fields)


GenerateClientLoginRequestBody = generate_client_login_request_body


def get_client_login_token_string(http_body):
  """Returns the token value for a ClientLoginToken.

  Reads the token from the server's response to a Client Login request and
  creates the token value string to use in requests.

  Args:
    http_body: str The body of the server's HTTP response to a Client Login
        request

  Returns:
    The token value string for a ClientLoginToken.
  """
  for response_line in http_body.splitlines():
    if response_line.startswith('Auth='):
      # Strip off the leading Auth= and return the Authorization value.
      return response_line[5:]
  return None


GetClientLoginTokenString = get_client_login_token_string


def get_captcha_challenge(http_body,
    captcha_base_url='http://www.google.com/accounts/'):
  """Returns the URL and token for a CAPTCHA challenge issued by the server.

  Args:
    http_body: str The body of the HTTP response from the server which
        contains the CAPTCHA challenge.
    captcha_base_url: str This function returns a full URL for viewing the
        challenge image which is built from the server's response. This
        base_url is used as the beginning of the URL because the server
        only provides the end of the URL. For example the server provides
        'Captcha?ctoken=Hi...N' and the URL for the image is
        'http://www.google.com/accounts/Captcha?ctoken=Hi...N'

  Returns:
    A dictionary containing the information needed to repond to the CAPTCHA
    challenge, the image URL and the ID token of the challenge. The
    dictionary is in the form:
    {'token': string identifying the CAPTCHA image,
     'url': string containing the URL of the image}
    Returns None if there was no CAPTCHA challenge in the response.
  """
  contains_captcha_challenge = False
  captcha_parameters = {}
  for response_line in http_body.splitlines():
    if response_line.startswith('Error=CaptchaRequired'):
      contains_captcha_challenge = True
    elif response_line.startswith('CaptchaToken='):
      # Strip off the leading CaptchaToken=
      captcha_parameters['token'] = response_line[13:]
    elif response_line.startswith('CaptchaUrl='):
      captcha_parameters['url'] = '%s%s' % (captcha_base_url,
          response_line[11:])
  if contains_captcha_challenge:
    return captcha_parameters
  else:
    return None


GetCaptchaChallenge = get_captcha_challenge


class ClientLoginToken(object):

  def __init__(self, token_string):
    self.token_string = token_string

  def modify_request(self, http_request):
    http_request.headers['Authorization'] = '%s%s' % (PROGRAMMATIC_AUTH_LABEL,
        self.token_string)

  ModifyRequest = modify_request



def _join_token_parts(*args):
  """"Escapes and combines all strings passed in.

  Used to convert a token object's members into a string instead of
  using pickle.

  Note: A None value will be converted to an empty string.

  Returns:
    A string in the form 1x|member1|member2|member3...
  """
  return '|'.join([urllib.quote_plus(a or '') for a in args])


def _split_token_parts(blob):
  """Extracts and unescapes fields from the provided binary string.

  Reverses the packing performed by _join_token_parts. Used to extract
  the members of a token object.

  Note: An empty string from the blob will be interpreted as None.

  Args:
    blob: str A string of the form 1x|member1|member2|member3 as created
        by _join_token_parts

  Returns:
    A list of unescaped strings.
  """
  return [urllib.unquote_plus(part) or None for part in blob.split('|')]


def token_to_blob(token):
  """Serializes the token data as a string for storage in a datastore.

  Supported token classes: ClientLoginToken, AuthSubToken, SecureAuthSubToken,
  OAuthRsaToken, and OAuthHmacToken, TwoLeggedOAuthRsaToken,
  TwoLeggedOAuthHmacToken.

  Args:
    token: A token object which must be of one of the supported token classes.

  Raises:
    UnsupportedTokenType if the token is not one of the supported token
    classes listed above.

  Returns:
    A string represenging this token. The string can be converted back into
    an equivalent token object using token_from_blob. Note that any members
    which are set to '' will be set to None when the token is deserialized
    by token_from_blob.
  """
  if isinstance(token, ClientLoginToken):
    return _join_token_parts('1c', token.token_string)
  else:
    raise UnsupportedTokenType(
        'Unable to serialize token of type %s' % type(token))


TokenToBlob = token_to_blob


def token_from_blob(blob):
  """Deserializes a token string from the datastore back into a token object.

  Supported token classes: ClientLoginToken, AuthSubToken, SecureAuthSubToken,
  OAuthRsaToken, and OAuthHmacToken, TwoLeggedOAuthRsaToken,
  TwoLeggedOAuthHmacToken.

  Args:
    blob: string created by token_to_blob.

  Raises:
    UnsupportedTokenType if the token is not one of the supported token
    classes listed above.

  Returns:
    A new token object with members set to the values serialized in the
    blob string. Note that any members which were set to '' in the original
    token will now be None.
  """
  parts = _split_token_parts(blob)
  if parts[0] == '1c':
    return ClientLoginToken(parts[1])
  else:
    raise UnsupportedTokenType(
        'Unable to deserialize token with type marker of %s' % parts[0])


TokenFromBlob = token_from_blob


def dump_tokens(tokens):
  return ','.join([token_to_blob(t) for t in tokens])


def load_tokens(blob):
  return [token_from_blob(s) for s in blob.split(',')]


def find_scopes_for_services(service_names=None):
  """Creates a combined list of scope URLs for the desired services.

  This method searches the AUTH_SCOPES dictionary.
  
  Args:
    service_names: list of strings (optional) Each name must be a key in the
                   AUTH_SCOPES dictionary. If no list is provided (None) then
                   the resulting list will contain all scope URLs in the
                   AUTH_SCOPES dict.

  Returns:
    A list of URL strings which are the scopes needed to access these services
    when requesting a token using AuthSub or OAuth.
  """
  result_scopes = []
  if service_names is None:
    for service_name, scopes in AUTH_SCOPES.iteritems():
      result_scopes.extend(scopes)
  else:
    for service_name in service_names:
      result_scopes.extend(AUTH_SCOPES[service_name])
  return result_scopes


FindScopesForServices = find_scopes_for_services



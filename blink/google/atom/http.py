#
# Copyright (C) 2008 Google Inc.
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


"""HttpClients in this module use httplib to make HTTP requests.

This module make HTTP requests based on httplib, but there are environments
in which an httplib based approach will not work (if running in Google App
Engine for example). In those cases, higher level classes (like AtomService
and GDataService) can swap out the HttpClient to transparently use a 
different mechanism for making HTTP requests.

  HttpClient: Contains a request method which performs an HTTP call to the 
      server.
      
  ProxiedHttpClient: Contains a request method which connects to a proxy using
      settings stored in operating system environment variables then 
      performs an HTTP call to the endpoint server.
"""


__author__ = 'api.jscudder (Jeff Scudder)'

import base64
import types
import os

from blink.google.atom import http_core as atom_http_core, http_interface as atom_http_interface, url as atom_url

from eventlib.green import httplib, socket

ssl_imported = False
ssl = None
try:
  import ssl
  ssl_imported = True
except ImportError:
  pass
  


class ProxyError(atom_http_interface.Error):
  pass


class TestConfigurationError(Exception):
  pass


DEFAULT_CONTENT_TYPE = 'application/atom+xml'


class HttpClient(atom_http_interface.GenericHttpClient):
  # Added to allow old v1 HttpClient objects to use the new 
  # http_code.HttpClient. Used in unit tests to inject a mock client.
  v2_http_client = None

  def __init__(self, headers=None):
    self.debug = False
    self.headers = headers or {}

  def request(self, operation, url, data=None, headers=None):
    """Performs an HTTP call to the server, supports GET, POST, PUT, and 
    DELETE.

    Usage example, perform and HTTP GET on http://www.google.com/:
      import atom.http
      client = atom.http.HttpClient()
      http_response = client.request('GET', 'http://www.google.com/')

    Args:
      operation: str The HTTP operation to be performed. This is usually one
          of 'GET', 'POST', 'PUT', or 'DELETE'
      data: filestream, list of parts, or other object which can be converted
          to a string. Should be set to None when performing a GET or DELETE.
          If data is a file-like object which can be read, this method will 
          read a chunk of 100K bytes at a time and send them. 
          If the data is a list of parts to be sent, each part will be 
          evaluated and sent.
      url: The full URL to which the request should be sent. Can be a string
          or atom_url.Url.
      headers: dict of strings. HTTP headers which should be sent
          in the request. 
    """
    all_headers = self.headers.copy()
    if headers:
      all_headers.update(headers)

    # If the list of headers does not include a Content-Length, attempt to
    # calculate it based on the data object.
    if data and 'Content-Length' not in all_headers:
      if isinstance(data, types.StringTypes):
        all_headers['Content-Length'] = str(len(data))
      else:
        raise atom_http_interface.ContentLengthRequired('Unable to calculate '
            'the length of the data parameter. Specify a value for '
            'Content-Length')

    # Set the content type to the default value if none was set.
    if 'Content-Type' not in all_headers:
      all_headers['Content-Type'] = DEFAULT_CONTENT_TYPE

    if self.v2_http_client is not None:
      http_request = atom_http_core.HttpRequest(method=operation)
      atom_http_core.Uri.parse_uri(str(url)).modify_request(http_request)
      http_request.headers = all_headers
      if data:
        http_request._body_parts.append(data)
      return self.v2_http_client.request(http_request=http_request)

    if not isinstance(url, atom_url.Url):
      if isinstance(url, types.StringTypes):
        url = atom_url.parse_url(url)
      else:
        raise atom_http_interface.UnparsableUrlObject('Unable to parse url '
            'parameter because it was not a string or atom_url.Url')
    
    connection = self._prepare_connection(url, all_headers)

    if self.debug:
      connection.debuglevel = 1

    connection.putrequest(operation, self._get_access_url(url), 
        skip_host=True)
    if url.port is not None:
      connection.putheader('Host', '%s:%s' % (url.host, url.port))
    else:
      connection.putheader('Host', url.host)

    # Overcome a bug in Python 2.4 and 2.5
    # httplib.HTTPConnection.putrequest adding
    # HTTP request header 'Host: www.google.com:443' instead of
    # 'Host: www.google.com', and thus resulting the error message
    # 'Token invalid - AuthSub token has wrong scope' in the HTTP response.
    if (url.protocol == 'https' and int(url.port or 443) == 443 and
        hasattr(connection, '_buffer') and
        isinstance(connection._buffer, list)):
      header_line = 'Host: %s:443' % url.host
      replacement_header_line = 'Host: %s' % url.host
      try:
        connection._buffer[connection._buffer.index(header_line)] = (
            replacement_header_line)
      except ValueError:  # header_line missing from connection._buffer
        pass

    # Send the HTTP headers.
    for header_name in all_headers:
      connection.putheader(header_name, all_headers[header_name])
    connection.endheaders()

    # If there is data, send it in the request.
    if data:
      if isinstance(data, list):
        for data_part in data:
          _send_data_part(data_part, connection)
      else:
        _send_data_part(data, connection)

    # Return the HTTP Response from the server.
    return connection.getresponse()
    
  def _prepare_connection(self, url, headers):
    if not isinstance(url, atom_url.Url):
      if isinstance(url, types.StringTypes):
        url = atom_url.parse_url(url)
      else:
        raise atom_http_interface.UnparsableUrlObject('Unable to parse url '
            'parameter because it was not a string or atom_url.Url')
    if url.protocol == 'https':
      if not url.port:
        return httplib.HTTPSConnection(url.host)
      return httplib.HTTPSConnection(url.host, int(url.port))
    else:
      if not url.port:
        return httplib.HTTPConnection(url.host)
      return httplib.HTTPConnection(url.host, int(url.port))

  def _get_access_url(self, url):
    return url.to_string()


class ProxiedHttpClient(HttpClient):
  """Performs an HTTP request through a proxy.
  
  The proxy settings are obtained from enviroment variables. The URL of the 
  proxy server is assumed to be stored in the environment variables 
  'https_proxy' and 'http_proxy' respectively. If the proxy server requires
  a Basic Auth authorization header, the username and password are expected to 
  be in the 'proxy-username' or 'proxy_username' variable and the 
  'proxy-password' or 'proxy_password' variable.
  
  After connecting to the proxy server, the request is completed as in 
  HttpClient.request.
  """
  def _prepare_connection(self, url, headers):
    # XXX: Non working proxy support removed. -Saul
    return HttpClient._prepare_connection(self, url, headers)

  def _get_access_url(self, url):
    return url.to_string()


def _send_data_part(data, connection):
  if isinstance(data, types.StringTypes):
    connection.send(data)
    return
  # Check to see if data is a file-like object that has a read method.
  elif hasattr(data, 'read'):
    # Read the file and send it a chunk at a time.
    while 1:
      binarydata = data.read(100000)
      if binarydata == '': break
      connection.send(binarydata)
    return
  else:
    # The data object was not a file.
    # Try to convert to a string and send the data.
    connection.send(str(data))
    return

"""JupyterHub authenticator."""
import requests
import os
import json
import sys

from subprocess import check_output
from traitlets import Unicode, Int, List, Bool, Instance
from six.moves.urllib.parse import unquote
from tornado import web

try:
    from jupyterhub.services.auth import HubAuth as JupyterHubAuth
    JupyterHubAuth.__name__ = "JupyterHubAuth"
except ImportError:
    JupyterHubAuth = None

from .base import BaseAuth


class HubAuth(BaseAuth):
    """Jupyter hub authenticator."""

    graders = List([], config=True, help="List of JupyterHub user names allowed to grade.")

    if JupyterHubAuth:
        hub_authenticator = Instance(JupyterHubAuth)
    else:
        hub_authenticator = None

    def _hub_authenticator_default(self):
        auth = JupyterHubAuth(parent=self)
        auth.login_url = '/hub/login'
        auth.api_url = '{}/hub/api'.format(self.hubapi_base_url)
        auth.api_token = self.hubapi_token
        auth.cookie_name = self.hubapi_cookie
        return auth

    hub_base_url = Unicode(config=True, help="Base URL of the hub server.")
    def _hub_base_url_default(self):
        return 'http://{}:8000'.format(self._ip)

    hubapi_base_url = Unicode(config=True, help="Base URL of the hub server.")
    def _hubapi_base_url_default(self):
        return 'http://{}:8081'.format(self._ip)
    def _hubapi_base_url_changed(self, name, old, new):
        if self.hub_authenticator:
            self.hub_authenticator.api_url = '{}/hub/api'.format(new)

    hubapi_token = Unicode(config=True, help="""JupyterHub API auth token.  
        Generated by running `jupyterhub token`.  If not explicitly set,
        nbgrader will use $JPY_API_TOKEN as the API token.""")
    def _hubapi_token_default(self):
        return os.environ.get('JPY_API_TOKEN', '')
    def _hubapi_token_changed(self, name, old, new):
        if self.hub_authenticator:
            self.hub_authenticator.api_token = new

    hubapi_cookie = Unicode("jupyter-hub-token", config=True, help="Name of the cookie used by JupyterHub")
    def _hubapi_cookie_changed(self, name, old, new):
        if self.hub_authenticator:
            self.hub_authenticator.cookie_name = new

    proxy_base_url = Unicode(config=True, help="Base URL of the configurable-http-proxy server.")
    def _proxy_base_url_default(self):
        return 'http://{}:8001'.format(self._ip)

    proxy_token = Unicode(config=True, help="""JupyterHub configurable proxy 
        auth token.  If not explicitly set, nbgrader will use 
        $CONFIGPROXY_AUTH_TOKEN as the API token.""")
    def _proxy_token_default(self):
        return os.environ.get('CONFIGPROXY_AUTH_TOKEN', '')

    notebook_url_prefix = Unicode(None, config=True, allow_none=True, help="""
        Relative path of the formgrader with respect to the hub's user base
        directory.  No trailing slash. i.e. "Documents" or "Documents/notebooks". """)
    def _notebook_url_prefix_changed(self, name, old, new):
        self.notebook_url_prefix = new.strip('/')

    remap_url = Unicode(config=True, help="""Suffix appened to 
        `HubAuth.hub_base_url` to form the full URL to the formgrade server.  By
        default this is '/hub/{NbGrader.course_id}'.  Change this if you
        plan on running more than one formgrade server behind one JupyterHub
        instance.""")
    def _remap_url_default(self):
        return '/hub/nbgrader/' + self.parent.course_id
    def _remap_url_changed(self, name, old, new):
        self.remap_url = new.rstrip('/')

    connect_ip = Unicode('', config=True, help="""The formgrader ip address that
        JupyterHub should actually connect to. Useful for when the formgrader is
        running behind a proxy or inside a container.""")

    notebook_server_user = Unicode('', config=True, help="""The user that hosts
        the autograded notebooks. By default, this is just the user that is logged
        in, but if that user is an admin user and has the ability to access other
        users' servers, then this variable can be set, allowing them to access
        the notebook server with the autograded notebooks.""")

    def _config_changed(self, name, old, new):
        if 'proxy_address' in new.HubAuth:
            raise ValueError(
                "HubAuth.proxy_address is no longer a valid configuration "
                "option, please use HubAuth.proxy_base_url instead."
            )

        if 'proxy_port' in new.HubAuth:
            raise ValueError(
                "HubAuth.proxy_port is no longer a valid configuration "
                "option, please use HubAuth.proxy_base_url instead."
            )

        if 'hub_address' in new.HubAuth:
            raise ValueError(
                "HubAuth.hub_address is no longer a valid configuration "
                "option, please use HubAuth.hub_base_url instead."
            )

        if 'hub_port' in new.HubAuth:
            raise ValueError(
                "HubAuth.hub_port is no longer a valid configuration "
                "option, please use HubAuth.hub_base_url instead."
            )

        if 'hubapi_address' in new.HubAuth:
            raise ValueError(
                "HubAuth.hubapi_address is no longer a valid configuration "
                "option, please use HubAuth.hubapi_base_url instead."
            )

        if 'hubapi_port' in new.HubAuth:
            raise ValueError(
                "HubAuth.hubapi_port is no longer a valid configuration "
                "option, please use HubAuth.hubapi_base_url instead."
            )

        super(HubAuth, self)._config_changed(name, old, new)

    def __init__(self, *args, **kwargs):
        super(HubAuth, self).__init__(*args, **kwargs)
        self._base_url = self.hub_base_url + self.remap_url
        self.register_with_proxy()

    @property
    def login_url(self):
        return self.hub_authenticator.login_url

    def register_with_proxy(self):
        # Register self as a route of the configurable-http-proxy and then
        # update the base_url to point to the new path.
        if self.connect_ip:
            ip = self.connect_ip
        else:
            ip = self._ip
        target = 'http://{}:{}'.format(ip, self._port)
        self.log.info("Proxying {} --> {}".format(self.remap_url, target))
        response = self._proxy_request('/api/routes' + self.remap_url, method='POST', body={
            'target': target
        })
        # This error will occur, for example, if the CONFIGPROXY_AUTH_TOKEN is
        # incorrect.
        if response.status_code != 201:
            raise Exception('Error while trying to add JupyterHub route. {}: {}'.format(response.status_code, response.text))

    def add_remap_url_prefix(self, url):
        if url == '/':
            return self.remap_url + '/?'
        else:
            return self.remap_url + url

    def transform_handler(self, handler):
        new_handler = list(handler)

        # transform the handler url
        url = self.add_remap_url_prefix(handler[0])
        new_handler[0] = url

        # transform any urls in the arguments
        if len(handler) > 2:
            new_args = handler[2].copy()
            if 'url' in new_args:
                new_args['url'] = self.add_remap_url_prefix(new_args['url'])
            new_handler[2] = new_args

        return tuple(new_handler)

    def get_user(self, handler):
        user_model = self.hub_authenticator.get_user(handler)
        if user_model:
            return user_model['name']
        return None

    def authenticate(self, user):
        """Authenticate a request.
        Returns a boolean or redirect."""

        # Check if the user name is registered as a grader.
        if user in self.graders:
            self._user = user
            return True

        self.log.warn('Unauthorized user "%s" attempted to access the formgrader.' % user)
        return False

    def notebook_server_exists(self):
        """Does the notebook server exist?"""
        if self.notebook_server_user:
            user = self.notebook_server_user
        else:
            user = self._user

        # first check if the server is running
        response = self._hubapi_request('/hub/api/users/{}'.format(user))
        if response.status_code == 200:
            user_data = response.json()
        else:
            self.log.warn("Could not access information about user {} (response: {} {})".format(
                user, response.status_code, response.reason))
            return False

        # start it if it's not running
        if user_data['server'] is None and user_data['pending'] != 'spawn':
            # start the server
            response = self._hubapi_request('/hub/api/users/{}/server'.format(user), method='POST')
            if response.status_code not in (201, 202):
                self.log.warn("Could not start server for user {} (response: {} {})".format(
                    user, response.status_code, response.reason))
                return False

        return True

    def get_notebook_server_cookie(self):
        # same user, so no need to request admin access
        if not self.notebook_server_user:
            return None

        # request admin access to the user's server
        response = self._hubapi_request('/hub/api/users/{}/admin-access'.format(self.notebook_server_user), method='POST')
        if response.status_code != 200:
            self.log.warn("Failed to gain admin access to user {}'s server (response: {} {})".format(
                self.notebook_server_user, response.status_code, response.reason))
            return None

        # access granted!
        cookie_name = '{}-{}'.format(self.hubapi_cookie, self.notebook_server_user)
        notebook_server_cookie = unquote(response.cookies[cookie_name][1:-1])
        cookie = {
            'name': cookie_name,
            'value': notebook_server_cookie,
            'path': '/user/{}'.format(self.notebook_server_user)
        }

        return cookie

    def get_notebook_url(self, relative_path):
        """Gets the notebook's url."""
        if self.notebook_url_prefix is not None:
            relative_path = self.notebook_url_prefix + '/' + relative_path
        if self.notebook_server_user:
            user = self.notebook_server_user
        else:
            user = self._user
        return "{}/user/{}/notebooks/{}".format(self.hub_base_url, user, relative_path)

    def _hubapi_request(self, *args, **kwargs):
        return self._request('hubapi', *args, **kwargs)

    def _proxy_request(self, *args, **kwargs):
        return self._request('proxy', *args, **kwargs)

    def _request(self, service, relative_path, method='GET', body=None):
        base_url = getattr(self, '%s_base_url' % service)
        token = getattr(self, '%s_token' % service)

        data = body
        if isinstance(data, (dict,)):
            data = json.dumps(data)

        return requests.request(method, base_url + relative_path, headers={
            'Authorization': 'token %s' % token
        }, data=data)

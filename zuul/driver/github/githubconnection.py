# Copyright 2015 Hewlett-Packard Development Company, L.P.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import collections
import concurrent.futures
import datetime
import logging
import hmac
import hashlib
import queue
import threading
import time
import re
import json

import cherrypy
import cachecontrol
from cachecontrol.cache import DictCache
from cachecontrol.heuristics import BaseHeuristic
import cachetools
import iso8601
import jwt
import requests
import github3
import github3.exceptions

from zuul.connection import BaseConnection
from zuul.lib.gearworker import ZuulGearWorker
from zuul.web.handler import BaseWebController
from zuul.lib.logutil import get_annotated_logger
from zuul.model import Ref, Branch, Tag, Project
from zuul.exceptions import MergeFailure
from zuul.driver.github.githubmodel import PullRequest, GithubTriggerEvent

GITHUB_BASE_URL = 'https://api.github.com'
PREVIEW_JSON_ACCEPT = 'application/vnd.github.machine-man-preview+json'


def _sign_request(body, secret):
    signature = 'sha1=' + hmac.new(
        secret.encode('utf-8'), body, hashlib.sha1).hexdigest()
    return signature


class UTC(datetime.tzinfo):
    """UTC"""

    def utcoffset(self, dt):
        return datetime.timedelta(0)

    def tzname(self, dt):
        return "UTC"

    def dst(self, dt):
        return datetime.timedelta(0)


utc = UTC()


class GithubRequestLogger:

    def __init__(self, zuul_event_id):
        log = logging.getLogger("zuul.GithubRequest")
        self.log = get_annotated_logger(log, zuul_event_id)

    def log_request(self, response, *args, **kwargs):
        self.log.debug('%s %s result: %s, size: %s, duration: %s',
                       response.request.method, response.url,
                       response.status_code, len(response.content),
                       int(response.elapsed.microseconds / 1000))


class GithubRateLimitHandler:
    """
    The GithubRateLimitHandler supplies the method handle_response that can be
    added to the requests session hooks. It will transparently catch API rate
    limit triggered 403 responses from github and retry the request after the
    wait time github tells us.
    """

    def __init__(self, github, log_rate_limit, zuul_event_id):
        log = logging.getLogger("zuul.GithubRateLimitHandler")
        self.log = get_annotated_logger(log, zuul_event_id)
        self.github = github
        self.rate_limit_logging_enabled = log_rate_limit

    def _log_rate_limit(self, response):
        if not self.rate_limit_logging_enabled:
            return

        rate_limit_remaining = response.headers.get('x-ratelimit-remaining')
        rate_limit_reset = response.headers.get('x-ratelimit-reset')

        # Determine rate limit resource from the path.
        path = response.request.path_url
        if path.startswith('/api/v3'):
            path = path[len('/api/v3'):]
        if path.startswith('/search/'):
            rate_limit_resource = 'search'
        else:
            rate_limit_resource = 'core'

        # Log the rate limits if enabled.
        if self.github._zuul_user_id:
            self.log.debug(
                'GitHub API rate limit (%s, %s) resource: %s, '
                'remaining: %s, reset: %s',
                self.github._zuul_project, self.github._zuul_user_id,
                rate_limit_resource, rate_limit_remaining, rate_limit_reset)
        else:
            self.log.debug(
                'GitHub API rate limit resource: %s, '
                'remaining: %s, reset: %s',
                rate_limit_resource, rate_limit_remaining, rate_limit_reset)

    def _handle_rate_limit(self, response):
        # We've hit the rate limit so calculate the time we need to wait based
        # on the x-ratelimit-reset header. After waiting we can retry the
        # original request and return it to the caller.
        reset = response.headers.get('x-ratelimit-reset')
        wait_time = int(reset) - int(time.time()) + 1
        self.log.warning('API rate limit reached, need to wait for '
                         '%s seconds', wait_time)
        time.sleep(wait_time)
        return self.github.session.send(response.request)

    def _handle_abuse(self, response):
        try:
            retry_after = int(response.headers.get('retry-after'))
        except Exception:
            # This should not happen but if it does we cannot handle it.
            # In this case the caller will need to handle the 403.
            self.log.error('Missing retry-after header while trying to handle '
                           'abuse error.')
            return response
        self.log.error('We triggered abuse detection, need to wait for '
                       '%s seconds', retry_after)
        time.sleep(retry_after + 1)
        return self.github.session.send(response.request)

    def handle_response(self, response, *args, **kwargs):

        rate_limit = response.headers.get('x-ratelimit-limit')

        if rate_limit:
            self._log_rate_limit(response)

        # If we got a 403 we could potentially have hit the rate limit. For
        # any other response we're finished here.
        if response.status_code != 403:
            return

        # Decode the body and check if we hit the rate limit.
        try:
            body = json.loads(response.content)
            message = body.get('message', '')

            # Catch rate limit and abuse detection responses. Every other 403
            # needs to be handled by the caller.
            if message.startswith('API rate limit exceeded'):
                return self._handle_rate_limit(response)
            elif message.startswith('You have triggered an abuse detection'):
                return self._handle_abuse(response)
        except Exception:
            # If we cannot decode the response body, log it here and return so
            # the caller can handle the response.
            self.log.exception("Couldn't json decode the response body.")


class GithubRetryHandler:
    """
    The GithubRetrHandler supplies the method handle_response that can be added
    to the requests session hooks. It will transparently handle 5xx errors on
    GET requests and retry them using an exponential backoff.
    """

    def __init__(self, github, retries, max_delay, zuul_event_id):
        log = logging.getLogger("zuul.GithubRetryHandler")
        self.log = get_annotated_logger(log, zuul_event_id)

        self.github = github
        self.max_retries = retries
        self.max_delay = max_delay
        self.initial_delay = 5

    def handle_response(self, response, *args, **kwargs):
        # Only handle GET requests that failed with 5xx. Retrying other request
        # types like POST can be dangerous because we cannot know if they
        # already might have altered the state on the server side.
        if response.request.method != 'GET':
            return
        if not 500 <= response.status_code < 600:
            return

        if hasattr(response.request, 'zuul_retry_count'):
            retry_count = response.request.zuul_retry_count
            retry_delay = min(response.request.zuul_retry_delay * 2,
                              self.max_delay)
        else:
            retry_count = 0
            retry_delay = self.initial_delay

        if retry_count >= self.max_retries:
            # We've reached the max retries so let the caller handle thr 503.
            self.log.error('GET Request failed with %s (%s/%s retries), '
                           'won\'t retry again.', response.status_code,
                           retry_count, self.max_retries)
            return

        self.log.warning('GET Request failed with %s (%s/%s retries), '
                         'retrying in %s seconds', response.status_code,
                         retry_count, self.max_retries, retry_delay)
        time.sleep(retry_delay)

        # Store retry information in the request object and perform the retry.
        retry_count += 1
        response.request.zuul_retry_count = retry_count
        response.request.zuul_retry_delay = retry_delay
        return self.github.session.send(response.request)


class GithubShaCache(object):
    def __init__(self):
        self.projects = {}

    def update(self, project_name, pr):
        project_cache = self.projects.setdefault(
            project_name,
            # Cache up to 4k shas for each project
            # Note we cache the actual sha for a PR and the
            # merge_commit_sha so we make this fairly large.
            cachetools.LRUCache(4096)
        )
        sha = pr['head']['sha']
        number = pr['number']
        cached_prs = project_cache.setdefault(sha, set())
        cached_prs.add(number)
        merge_commit_sha = pr.get('merge_commit_sha')
        if merge_commit_sha:
            cached_prs = project_cache.setdefault(merge_commit_sha, set())
            cached_prs.add(number)

    def get(self, project_name, sha):
        project_cache = self.projects.get(project_name, {})
        cached_prs = project_cache.get(sha, set())
        return cached_prs


class GithubGearmanWorker(object):
    """A thread that answers gearman requests"""
    log = logging.getLogger("zuul.GithubGearmanWorker")

    def __init__(self, connection):
        self.config = connection.sched.config
        self.connection = connection

        handler = "github:%s:payload" % self.connection.connection_name
        self.jobs = {
            handler: self.handle_payload,
        }

        self.gearworker = ZuulGearWorker(
            'Zuul Github Connector',
            'zuul.GithubGearmanWorker',
            'github-gearman-worker',
            self.config,
            self.jobs)

    def handle_payload(self, job):
        args = json.loads(job.arguments)
        headers = args.get("headers")
        body = args.get("body")

        delivery = headers.get('x-github-delivery')
        log = get_annotated_logger(self.log, delivery)
        log.debug("Github Webhook Received")

        # TODO(jlk): Validate project in the request is a project we know

        try:
            self.__dispatch_event(body, headers, log)
            output = {'return_code': 200}
        except Exception:
            output = {'return_code': 503}
            log.exception("Exception handling Github event:")

        job.sendWorkComplete(json.dumps(output))

    def __dispatch_event(self, body, headers, log):
        try:
            event = headers['x-github-event']
            log.debug("X-Github-Event: " + event)
        except KeyError:
            log.debug("Request headers missing the X-Github-Event.")
            raise Exception('Please specify a X-Github-Event header.')

        delivery = headers.get('x-github-delivery')
        try:
            self.connection.addEvent(body, event, delivery)
        except Exception:
            message = 'Exception deserializing JSON body'
            log.exception(message)
            # TODO(jlk): Raise this as something different?
            raise Exception(message)

    def start(self):
        self.gearworker.start()

    def stop(self):
        self.gearworker.stop()


class GithubEventProcessor(object):
    def __init__(self, connector, event_tuple):
        self.connector = connector
        self.connection = connector.connection
        self.ts, self.body, self.event_type, self.delivery = event_tuple
        logger = logging.getLogger("zuul.GithubEventProcessor")
        self.zuul_event_id = self.delivery
        self.log = get_annotated_logger(logger, self.zuul_event_id)
        self.event = None

    def run(self):
        self.log.debug("Starting event processing, queue length %s",
                       self.connection.getEventQueueSize())
        try:
            self._process_event()
        finally:
            self.log.debug("Finished event processing")
            return self.event

    def _process_event(self):
        if self.connector._stopped:
            return

        # If there's any installation mapping information in the body then
        # update the project mapping before any requests are made.
        installation_id = self.body.get('installation', {}).get('id')
        project_name = self.body.get('repository', {}).get('full_name')

        if installation_id and project_name:
            old_id = self.connection.installation_map.get(project_name)

            if old_id and old_id != installation_id:
                msg = "Unexpected installation_id change for %s. %d -> %d."
                self.log.warning(msg, project_name, old_id, installation_id)

            self.connection.installation_map[project_name] = installation_id

        try:
            method = getattr(self, '_event_' + self.event_type)
        except AttributeError:
            # TODO(jlk): Gracefully handle event types we don't care about
            # instead of logging an exception.
            message = "Unhandled X-Github-Event: {0}".format(self.event_type)
            self.log.debug(message)
            # Returns empty on unhandled events
            return

        self.log.debug("Handling %s event", self.event_type)
        event = None
        try:
            event = method()
        except Exception:
            # NOTE(pabelanger): We should report back to the PR we could
            # not process the event, to give the user a chance to
            # retrigger.
            self.log.exception('Exception when handling event:')

        if event:

            # Note we limit parallel requests per installation id to avoid
            # triggering abuse detection.
            with self.connection.get_request_lock(installation_id):
                event.delivery = self.delivery
                event.zuul_event_id = self.delivery
                event.timestamp = self.ts
                project = self.connection.source.getProject(event.project_name)
                if event.change_number:
                    self.connection._getChange(project,
                                               event.change_number,
                                               event.patch_number,
                                               refresh=True,
                                               event=event)
                    self.log.debug("Refreshed change %s,%s",
                                   event.change_number, event.patch_number)

                # If this event references a branch and we're excluding
                # unprotected branches, we might need to check whether the
                # branch is now protected.
                if event.branch:
                    b = self.connection.getBranch(project.name, event.branch)
                    if b is not None:
                        branch_protected = b.get('protected')
                        self.connection.checkBranchCache(
                            project, event.branch, branch_protected, self.log)
                        event.branch_protected = branch_protected
                    else:
                        # This can happen if the branch was deleted in GitHub.
                        # In this case we assume that the branch COULD have
                        # been protected before. The cache update is handled by
                        # the push event, so we don't touch the cache here
                        # again.
                        event.branch_protected = True

            event.project_hostname = self.connection.canonical_hostname
            self.event = event

    def _event_push(self):
        base_repo = self.body.get('repository')

        event = GithubTriggerEvent()
        event.trigger_name = 'github'
        event.project_name = base_repo.get('full_name')
        event.type = 'push'
        event.branch_updated = True

        event.ref = self.body.get('ref')
        event.oldrev = self.body.get('before')
        event.newrev = self.body.get('after')
        event.commits = self.body.get('commits')

        ref_parts = event.ref.split('/', 2)  # ie, ['refs', 'heads', 'foo/bar']

        if ref_parts[1] == "heads":
            # necessary for the scheduler to match against particular branches
            event.branch = ref_parts[2]

        # This checks whether the event created or deleted a branch so
        # that Zuul may know to perform a reconfiguration on the
        # project.
        if event.oldrev == '0' * 40:
            event.branch_created = True
        if event.newrev == '0' * 40:
            event.branch_deleted = True

        if event.branch:
            project = self.connection.source.getProject(event.project_name)
            if event.branch_deleted:
                # We currently cannot determine if a deleted branch was
                # protected so we need to assume it was. GitHub doesn't allow
                # deletion of protected branches but we don't get a
                # notification about branch protection settings. Thus we don't
                # know if branch protection has been disabled before deletion
                # of the branch.
                # FIXME(tobiash): Find a way to handle that case
                self.connection._clearBranchCache(project, self.log)
            elif event.branch_created:
                # A new branch never can be protected because that needs to be
                # configured after it has been created.
                self.connection._clearBranchCache(project, self.log)

        return event

    def _event_pull_request(self):
        action = self.body.get('action')
        pr_body = self.body.get('pull_request')

        event = self._pull_request_to_event(pr_body)
        event.account = self._get_sender(self.body)

        event.type = 'pull_request'
        if action == 'opened':
            event.action = 'opened'
        elif action == 'synchronize':
            event.action = 'changed'
        elif action == 'closed':
            event.action = 'closed'
        elif action == 'reopened':
            event.action = 'reopened'
        elif action == 'labeled':
            event.action = 'labeled'
            event.label = self.body['label']['name']
        elif action == 'unlabeled':
            event.action = 'unlabeled'
            event.label = self.body['label']['name']
        elif action == 'edited':
            event.action = 'edited'
        else:
            return None

        return event

    def _event_issue_comment(self):
        """Handles pull request comments"""
        action = self.body.get('action')
        if action != 'created':
            return
        if not self.body.get('issue', {}).get('pull_request'):
            # Do not process non-PR issue comment
            return
        pr_body = self._issue_to_pull_request(self.body)
        if pr_body is None:
            return

        event = self._pull_request_to_event(pr_body)
        event.account = self._get_sender(self.body)
        event.comment = self.body.get('comment').get('body')
        event.type = 'pull_request'
        event.action = 'comment'
        return event

    def _event_pull_request_review(self):
        """Handles pull request reviews"""
        pr_body = self.body.get('pull_request')
        if pr_body is None:
            return

        review = self.body.get('review')
        if review is None:
            return

        event = self._pull_request_to_event(pr_body)
        event.state = review.get('state')
        event.account = self._get_sender(self.body)
        event.type = 'pull_request_review'
        event.action = self.body.get('action')
        return event

    def _event_status(self):
        action = self.body.get('action')
        if action == 'pending':
            return
        project = self.body.get('name')
        pr_body = self.connection.getPullBySha(
            self.body['sha'], project, self.zuul_event_id)
        if pr_body is None:
            return

        event = self._pull_request_to_event(pr_body)
        event.account = self._get_sender(self.body)
        event.type = 'pull_request'
        event.action = 'status'
        # Github API is silly. Webhook blob sets author data in
        # 'sender', but API call to get status puts it in 'creator'.
        # Duplicate the data so our code can look in one place
        self.body['creator'] = self.body['sender']
        event.status = "%s:%s:%s" % _status_as_tuple(self.body)
        return event

    def _issue_to_pull_request(self, body):
        number = body.get('issue').get('number')
        project_name = body.get('repository').get('full_name')
        pr_body, pr_obj = self.connection.getPull(
            project_name, number, self.zuul_event_id)
        if pr_body is None:
            self.log.debug('Pull request #%s not found in project %s' %
                           (number, project_name))
        return pr_body

    def _pull_request_to_event(self, pr_body):
        event = GithubTriggerEvent()
        event.trigger_name = 'github'

        base = pr_body.get('base')
        base_repo = base.get('repo')
        head = pr_body.get('head')

        event.project_name = base_repo.get('full_name')
        event.change_number = pr_body.get('number')
        event.change_url = self.connection.getPullUrl(event.project_name,
                                                      event.change_number)
        event.updated_at = pr_body.get('updated_at')
        event.branch = base.get('ref')
        event.ref = "refs/pull/" + str(pr_body.get('number')) + "/head"
        event.patch_number = head.get('sha')

        event.title = pr_body.get('title')

        return event

    def _get_sender(self, body):
        login = body.get('sender').get('login')
        if login:
            # TODO(tobiash): it might be better to plumb in the installation id
            project = body.get('repository', {}).get('full_name')
            user = self.connection.getUser(login, project)
            self.log.debug("Got user %s", user)
            return user


class GithubEventConnector:
    """Move events from GitHub into the scheduler"""

    log = logging.getLogger("zuul.GithubEventConnector")

    def __init__(self, connection):
        self.connection = connection
        self._stopped = False
        self._event_dispatcher = threading.Thread(
            name='GithubEventDispatcher', target=self.run_event_dispatcher,
            daemon=True)
        self._event_forwarder = threading.Thread(
            name='GithubEventForwarder', target=self.run_event_forwarder,
            daemon=True)
        self._thread_pool = concurrent.futures.ThreadPoolExecutor()
        self._event_forward_queue = queue.Queue()

    def stop(self):
        self._stopped = True
        self.connection.addEvent(None)
        self._event_dispatcher.join()

        self._event_forward_queue.put(None)
        self._event_forwarder.join()
        self._thread_pool.shutdown()

    def start(self):
        self._event_forwarder.start()
        self._event_dispatcher.start()

    def run_event_dispatcher(self):
        while True:
            if self._stopped:
                return
            try:
                data = self.connection.getEvent()
                processor = GithubEventProcessor(self, data)
                future = self._thread_pool.submit(processor.run)
                self._event_forward_queue.put(future)
            except Exception:
                self.log.exception("Exception moving GitHub event:")
            finally:
                self.connection.eventDone()

    def run_event_forwarder(self):
        while True:
            if self._stopped:
                return
            try:
                future = self._event_forward_queue.get()
                if future is None:
                    return
                event = future.result()
                if event:
                    self.connection.logEvent(event)
                    self.connection.sched.addEvent(event)
            except Exception:
                self.log.exception("Exception moving GitHub event:")
            finally:
                self._event_forward_queue.task_done()


class GithubUser(collections.Mapping):
    log = logging.getLogger('zuul.GithubUser')

    def __init__(self, username, connection, project):
        self._connection = connection
        self._username = username
        self._data = None
        self._project = project

    def __getitem__(self, key):
        self._init_data()
        return self._data[key]

    def __iter__(self):
        self._init_data()
        return iter(self._data)

    def __len__(self):
        self._init_data()
        return len(self._data)

    def _init_data(self):
        if self._data is None:
            github = self._connection.getGithubClient(self._project)
            user = github.user(self._username)
            self.log.debug("Initialized data for user %s", self._username)
            self._data = {
                'username': user.login,
                'name': user.name,
                'email': user.email,
                'html_url': user.html_url,
            }


class GithubConnection(BaseConnection):
    driver_name = 'github'
    log = logging.getLogger("zuul.GithubConnection")
    payload_path = 'payload'

    def __init__(self, driver, connection_name, connection_config):
        super(GithubConnection, self).__init__(
            driver, connection_name, connection_config)
        self._change_cache = {}
        self._change_update_lock = {}
        self._project_branch_cache_include_unprotected = {}
        self._project_branch_cache_exclude_unprotected = {}
        self.projects = {}
        self.git_ssh_key = self.connection_config.get('sshkey')
        self.server = self.connection_config.get('server', 'github.com')
        self.canonical_hostname = self.connection_config.get(
            'canonical_hostname', self.server)
        self.source = driver.getSource(self)
        self.event_queue = queue.Queue()
        self._sha_pr_cache = GithubShaCache()

        self._request_locks = {}
        self.max_threads_per_installation = int(self.connection_config.get(
            'max_threads_per_installation', 1))

        # Logging of rate limit is optional as this does additional requests
        rate_limit_logging = self.connection_config.get(
            'rate_limit_logging', 'true')
        self._log_rate_limit = True
        if rate_limit_logging.lower() == 'false':
            self._log_rate_limit = False

        if self.server == 'github.com':
            self.base_url = GITHUB_BASE_URL
        else:
            self.base_url = 'https://%s/api/v3' % self.server

        # ssl verification must default to true
        verify_ssl = self.connection_config.get('verify_ssl', 'true')
        self.verify_ssl = True
        if verify_ssl.lower() == 'false':
            self.verify_ssl = False

        self._github = None
        self.app_id = None
        self.app_key = None
        self.sched = None

        self.installation_map = {}
        self.installation_token_cache = {}

        # NOTE(jamielennox): Better here would be to cache to memcache or file
        # or something external - but zuul already sucks at restarting so in
        # memory probably doesn't make this much worse.

        # NOTE(tobiash): Unlike documented cachecontrol doesn't priorize
        # the etag caching but doesn't even re-request until max-age was
        # elapsed.
        #
        # Thus we need to add a custom caching heuristic which simply drops
        # the cache-control header containing max-age. This way we force
        # cachecontrol to only rely on the etag headers.
        #
        # http://cachecontrol.readthedocs.io/en/latest/etags.html
        # http://cachecontrol.readthedocs.io/en/latest/custom_heuristics.html
        class NoAgeHeuristic(BaseHeuristic):
            def update_headers(self, response):
                if 'cache-control' in response.headers:
                    del response.headers['cache-control']

        self.cache_adapter = cachecontrol.CacheControlAdapter(
            DictCache(),
            cache_etags=True,
            heuristic=NoAgeHeuristic())

        # The regex is based on the connection host. We do not yet support
        # cross-connection dependency gathering
        self.depends_on_re = re.compile(
            r"^Depends-On: https://%s/.+/.+/pull/[0-9]+$" % self.server,
            re.MULTILINE | re.IGNORECASE)

    def toDict(self):
        d = super().toDict()
        d.update({
            "baseurl": self.baseurl,
            "canonical_hostname": self.canonical_hostname,
            "server": self.server,
        })
        return d

    def onLoad(self):
        self.log.info('Starting GitHub connection: %s' % self.connection_name)
        self.gearman_worker = GithubGearmanWorker(self)
        self.log.info('Authing to GitHub')
        self._authenticateGithubAPI()
        self._prime_installation_map()
        self.log.info('Starting event connector')
        self._start_event_connector()
        self.log.info('Starting GearmanWorker')
        self.gearman_worker.start()

    def onStop(self):
        # TODO(jeblair): remove this check which is here only so that
        # zuul-web can call connections.stop to shut down the sql
        # connection.
        if hasattr(self, 'gearman_worker'):
            self.gearman_worker.stop()
            self._stop_event_connector()

    def _start_event_connector(self):
        self.github_event_connector = GithubEventConnector(self)
        self.github_event_connector.start()

    def _stop_event_connector(self):
        if self.github_event_connector:
            self.github_event_connector.stop()

    def _createGithubClient(self, zuul_event_id=None):
        if self.server != 'github.com':
            url = 'https://%s/' % self.server
            if not self.verify_ssl:
                # disabling ssl verification is evil so emit a warning
                self.log.warning("SSL verification disabled for "
                                 "GitHub Enterprise")
            github = github3.GitHubEnterprise(url, verify=self.verify_ssl)
        else:
            github = github3.GitHub(session=github3.session.GitHubSession(
                default_read_timeout=300))

        # anything going through requests to http/s goes through cache
        github.session.mount('http://', self.cache_adapter)
        github.session.mount('https://', self.cache_adapter)

        # Log all requests with attached event id
        request_logger = GithubRequestLogger(zuul_event_id)
        github.session.hooks['response'].append(request_logger.log_request)

        # Install hook for handling rate limit errors transparently
        rate_limit_handler = GithubRateLimitHandler(
            github, self._log_rate_limit, zuul_event_id)
        github.session.hooks['response'].append(
            rate_limit_handler.handle_response)

        # Install hook for handling retries of GET requests transparently
        retry_handler = GithubRetryHandler(github, 5, 30, zuul_event_id)
        github.session.hooks['response'].append(retry_handler.handle_response)

        # Add properties to store project and user for logging later
        github._zuul_project = None
        github._zuul_user_id = None
        return github

    def _authenticateGithubAPI(self):
        config = self.connection_config

        api_token = config.get('api_token')

        app_id = config.get('app_id')
        app_key = None
        app_key_file = config.get('app_key')

        self._github = self._createGithubClient()

        if api_token:
            self._github.login(token=api_token)

        if app_key_file:
            try:
                with open(app_key_file, 'r') as f:
                    app_key = f.read()
            except IOError:
                m = "Failed to open app key file for reading: %s"
                self.log.error(m, app_key_file)

        if (app_id or app_key) and \
                not (app_id and app_key):
            self.log.warning("You must provide an app_id and "
                             "app_key to use installation based "
                             "authentication")

            return

        if app_id:
            self.app_id = int(app_id)
        if app_key:
            self.app_key = app_key

    def _get_app_auth_headers(self):
        now = datetime.datetime.now(utc)
        expiry = now + datetime.timedelta(minutes=5)

        data = {'iat': now, 'exp': expiry, 'iss': self.app_id}
        app_token = jwt.encode(data,
                               self.app_key,
                               algorithm='RS256').decode('utf-8')

        headers = {'Accept': PREVIEW_JSON_ACCEPT,
                   'Authorization': 'Bearer %s' % app_token}

        return headers

    def _get_installation_key(self, project, inst_id=None,
                              reprime=False):
        installation_id = inst_id
        if project is not None:
            installation_id = self.installation_map.get(project)

        if not installation_id:
            if reprime:
                # prime installation map and try again without refreshing
                self._prime_installation_map()
                return self._get_installation_key(project,
                                                  inst_id=inst_id,
                                                  reprime=False)

            self.log.error("No installation ID available for project %s",
                           project)
            return ''

        now = datetime.datetime.now(utc)
        token, expiry = self.installation_token_cache.get(installation_id,
                                                          (None, None))

        if ((not expiry) or (not token) or (now >= expiry)):
            headers = self._get_app_auth_headers()

            url = "%s/installations/%s/access_tokens" % (self.base_url,
                                                         installation_id)

            response = requests.post(url, headers=headers, json=None)
            response.raise_for_status()

            data = response.json()

            expiry = iso8601.parse_date(data['expires_at'])
            expiry -= datetime.timedelta(minutes=2)
            token = data['token']

            self.installation_token_cache[installation_id] = (token, expiry)

        return token

    def _get_repos_of_installation(self, inst_id, headers):
        url = '%s/installation/repositories?per_page=100' % self.base_url
        project_names = []
        while url:
            self.log.debug("Fetching repos for install %s" % inst_id)
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            repos = response.json()

            for repo in repos.get('repositories'):
                project_name = repo.get('full_name')
                project_names.append(project_name)

            # check if we need to do further paged calls
            url = response.links.get('next', {}).get('url')
        return project_names

    def _prime_installation_map(self):
        """Walks each app install for the repos to prime install IDs"""

        if not self.app_id:
            return

        url = '%s/app/installations' % self.base_url
        installations = []
        headers = self._get_app_auth_headers()
        page = 1
        while url:
            self.log.debug("Fetching installations for GitHub app "
                           "(page %s)" % page)
            page += 1
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            installations.extend(response.json())

            # check if we need to do further paged calls
            url = response.links.get(
                'next', {}).get('url')

        headers_per_inst = {}
        with concurrent.futures.ThreadPoolExecutor() as executor:

            token_by_inst = {}
            for install in installations:
                inst_id = install.get('id')
                token_by_inst[inst_id] = executor.submit(
                    self._get_installation_key, project=None, inst_id=inst_id)

            for inst_id, result in token_by_inst.items():
                token = result.result()
                headers_per_inst[inst_id] = {
                    'Accept': PREVIEW_JSON_ACCEPT,
                    'Authorization': 'token %s' % token
                }

            project_names_by_inst = {}
            for install in installations:
                inst_id = install.get('id')
                headers = headers_per_inst[inst_id]

                project_names_by_inst[inst_id] = executor.submit(
                    self._get_repos_of_installation, inst_id, headers)

            for inst_id, result in project_names_by_inst.items():
                project_names = result.result()
                for project_name in project_names:
                    self.installation_map[project_name] = inst_id

    def get_request_lock(self, installation_id):
        return self._request_locks.setdefault(
            installation_id, threading.Semaphore(
                value=self.max_threads_per_installation))

    def addEvent(self, data, event=None, delivery=None):
        return self.event_queue.put((time.time(), data, event, delivery))

    def getEvent(self):
        return self.event_queue.get()

    def getEventQueueSize(self):
        return self.event_queue.qsize()

    def eventDone(self):
        self.event_queue.task_done()

    def getGithubClient(self,
                        project=None,
                        zuul_event_id=None):
        # if you're authenticating for a project and you're an integration then
        # you need to use the installation specific token.
        if project and self.app_id:
            github = self._createGithubClient(zuul_event_id)
            github.login(token=self._get_installation_key(project))
            github._zuul_project = project
            github._zuul_user_id = self.installation_map.get(project)
            return github

        # if we're using api_key authentication then this is already token
        # authenticated, if not then anonymous is the best we have.
        return self._createGithubClient(zuul_event_id)

    def maintainCache(self, relevant):
        remove = set()
        for key, change in self._change_cache.items():
            if change not in relevant:
                remove.add(key)
        for key in remove:
            del self._change_cache[key]

    def getChange(self, event, refresh=False):
        """Get the change representing an event."""

        project = self.source.getProject(event.project_name)
        if event.change_number:
            change = self._getChange(project, event.change_number,
                                     event.patch_number, refresh=refresh,
                                     event=event)
            if hasattr(event, 'change_url') and event.change_url:
                change.url = event.change_url
            else:
                # The event has no change url so just construct it
                change.url = self.getPullUrl(
                    event.project_name, event.change_number)
            change.uris = [
                '%s/%s/pull/%s' % (self.server, project, change.number),
            ]
            change.source_event = event
            change.is_current_patchset = (change.pr.get('head').get('sha') ==
                                          event.patch_number)
        else:
            if event.ref and event.ref.startswith('refs/tags/'):
                change = Tag(project)
                change.tag = event.ref[len('refs/tags/'):]
            elif event.ref and event.ref.startswith('refs/heads/'):
                change = Branch(project)
                change.branch = event.ref[len('refs/heads/'):]
            else:
                change = Ref(project)
            change.ref = event.ref
            change.oldrev = event.oldrev
            change.newrev = event.newrev
            change.url = self.getGitwebUrl(project, sha=event.newrev)
            change.source_event = event
            if hasattr(event, 'commits'):
                change.files = self.getPushedFileNames(event)
        return change

    def _getChange(self, project, number, patchset=None, refresh=False,
                   event=None):
        key = (project.name, number, patchset)
        change = self._change_cache.get(key)
        if change and not refresh:
            return change
        if not change:
            change = PullRequest(project.name)
            change.project = project
            change.number = number
            change.patchset = patchset
        self._change_cache[key] = change
        try:
            # This can be called multi-threaded during github event
            # preprocessing. In order to avoid data races perform locking
            # by cached key. Try to acquire the lock non-blocking at first.
            # If the lock is already taken we're currently updating the very
            # same chnange right now and would likely get the same data again.
            lock = self._change_update_lock.setdefault(key, threading.Lock())
            if lock.acquire(blocking=False):
                try:
                    self._updateChange(change, event)
                finally:
                    # We need to remove the lock here again so we don't leak
                    # them.
                    lock.release()
                    del self._change_update_lock[key]
            else:
                # We didn't get the lock so we don't need to update the same
                # change again, but to be correct we should at least wait until
                # the other thread is done updating the change.
                log = get_annotated_logger(self.log, event)
                log.debug("Change %s is currently being updated, "
                          "waiting for it to finish", change)
                with lock:
                    log.debug('Finished updating change %s', change)
        except Exception:
            if key in self._change_cache:
                del self._change_cache[key]
            raise
        return change

    def getChangesDependingOn(self, change, projects, tenant):
        changes = []
        if not change.uris:
            return changes

        # Get a list of projects with unique installation ids
        installation_ids = set()
        installation_projects = set()

        if projects:
            # We only need to find changes in projects in the supplied
            # ChangeQueue.  Find all of the github installations for
            # all of those projects, and search using each of them, so
            # that if we get the right results based on the
            # permissions granted to each of the installations.  The
            # common case for this is likely to be just one
            # installation -- change queues aren't likely to span more
            # than one installation.
            for project in projects:
                installation_id = self.installation_map.get(project.name)
                if installation_id not in installation_ids:
                    installation_ids.add(installation_id)
                    installation_projects.add(project.name)
        else:
            # We aren't in the context of a change queue and we just
            # need to query all installations of this tenant. This currently
            # only happens if certain features of the zuul trigger are
            # used; generally it should be avoided.
            for project_name, installation_id in self.installation_map.items():
                trusted, project = tenant.getProject(project_name)
                # ignore projects from different tenants
                if not project:
                    continue
                if installation_id not in installation_ids:
                    installation_ids.add(installation_id)
                    installation_projects.add(project_name)

        keys = set()
        pattern = ' OR '.join(change.uris)
        query = '%s type:pr is:open in:body' % pattern
        # Repeat the search for each installation id (project)
        for installation_project in installation_projects:
            github = self.getGithubClient(installation_project)
            for issue in github.search_issues(query=query):
                pr = issue.issue.pull_request().as_dict()
                if not pr.get('url'):
                    continue
                # the issue provides no good description of the project :\
                org, proj, _, num = pr.get('url').split('/')[-4:]
                proj = pr.get('base').get('repo').get('full_name')
                sha = pr.get('head').get('sha')
                key = (proj, num, sha)
                if key in keys:
                    continue
                self.log.debug("Found PR %s/%s needs %s/%s" %
                               (proj, num, change.project.name,
                                change.number))
                keys.add(key)
            self.log.debug("Ran search issues: %s", query)

        for key in keys:
            (proj, num, sha) = key
            project = self.source.getProject(proj)
            change = self._getChange(project, int(num), patchset=sha)
            changes.append(change)

        return changes

    def _updateChange(self, change, event):
        log = get_annotated_logger(self.log, event)
        log.info("Updating %s" % (change,))
        change.pr, pr_obj = self.getPull(
            change.project.name, change.number, event=event)
        change.ref = "refs/pull/%s/head" % change.number
        change.branch = change.pr.get('base').get('ref')

        # Don't overwrite the files list. The change object is bound to a
        # specific revision and thus the changed files won't change. This is
        # important if we got the files later because of the 300 files limit.
        if not change.files:
            change.files = change.pr.get('files')
        # Github's pull requests files API only returns at max
        # the first 300 changed files of a PR in alphabetical order.
        # https://developer.github.com/v3/pulls/#list-pull-requests-files
        if len(change.files) < change.pr.get('changed_files', 0):
            log.warning("Got only %s files but PR has %s files.",
                        len(change.files),
                        change.pr.get('changed_files', 0))
            # In this case explicitly set change.files to None to signalize
            # that we need to ask the mergers later in pipeline processing.
            # We cannot query the files here using the mergers because this
            # can slow down the github event queue considerably.
            change.files = None
        change.title = change.pr.get('title')
        change.open = change.pr.get('state') == 'open'

        # Never change the is_merged attribute back to unmerged. This is
        # crucial so this cannot race with mergePull wich sets this attribute
        # after a successful merge.
        if not change.is_merged:
            change.is_merged = change.pr.get('merged')

        change.status = self._get_statuses(
            change.project, change.patchset, event)
        change.reviews = self.getPullReviews(
            pr_obj, change.project, change.number, event)
        change.labels = change.pr.get('labels')
        # ensure message is at least an empty string
        message = change.pr.get("body") or ""
        if change.title:
            if message:
                message = "{}\n\n{}".format(change.title, message)
            else:
                message = change.title
        change.message = message

        # Note(tobiash): The updated_at timestamp is a moving target that is
        # not bound to the pull request 'version' we can solve that by just not
        # updating the timestamp if the pull request is updated in the cache.
        # This way the old pull request object retains its old timestamp and
        # the update check works.
        if not change.updated_at:
            change.updated_at = self._ghTimestampToDate(
                change.pr.get('updated_at'))
        change.url = change.pr.get('url')
        change.uris = [
            '%s/%s/pull/%s' % (self.server, change.project.name,
                               change.number),
        ]

        if self.sched:
            self.sched.onChangeUpdated(change, event)

        return change

    def getGitUrl(self, project: Project):
        if self.git_ssh_key:
            return 'ssh://git@%s/%s.git' % (self.server, project.name)

        # if app_id is configured but self.app_id is empty we are not
        # authenticated yet against github as app
        if not self.app_id and self.connection_config.get('app_id', None):
            self._authenticateGithubAPI()
            self._prime_installation_map()

        if self.app_id:
            # We may be in the context of a merger or executor here. The
            # mergers and executors don't receive webhook events so they miss
            # new repository installations. In order to cope with this we need
            # to reprime the installation map if we don't find the repo there.
            installation_key = self._get_installation_key(project.name,
                                                          reprime=True)
            return 'https://x-access-token:%s@%s/%s' % (installation_key,
                                                        self.server,
                                                        project.name)

        return 'https://%s/%s' % (self.server, project.name)

    def getGitwebUrl(self, project, sha=None):
        url = 'https://%s/%s' % (self.server, project)
        if sha is not None:
            url += '/commit/%s' % sha
        return url

    def getProject(self, name):
        return self.projects.get(name)

    def addProject(self, project):
        self.projects[project.name] = project

    def clearBranchCache(self):
        self._project_branch_cache_exclude_unprotected = {}
        self._project_branch_cache_include_unprotected = {}

    def getProjectBranches(self, project, tenant):
        exclude_unprotected = tenant.getExcludeUnprotectedBranches(project)
        if exclude_unprotected:
            cache = self._project_branch_cache_exclude_unprotected
        else:
            cache = self._project_branch_cache_include_unprotected

        branches = cache.get(project.name)
        if branches is not None:
            return branches

        github = self.getGithubClient(project.name)
        url = github.session.build_url('repos', project.name,
                                       'branches')

        headers = {'Accept': 'application/vnd.github.loki-preview+json'}
        params = {'per_page': 100}
        if exclude_unprotected:
            params['protected'] = 1

        branches = []
        while url:
            resp = github.session.get(
                url, headers=headers, params=params)

            # check if we need to do further paged calls
            url = resp.links.get('next', {}).get('url')

            if resp.status_code == 403:
                self.log.error(str(resp))
                rate_limit = github.rate_limit()
                if rate_limit['resources']['core']['remaining'] == 0:
                    self.log.warning(
                        "Rate limit exceeded, using empty branch list")
                return []
            elif resp.status_code == 404:
                raise Exception("Got status code 404 when listing branches "
                                "of project %s" % project.name)

            branches.extend([x['name'] for x in resp.json()])

        cache[project.name] = branches
        return branches

    def getBranch(self, project_name, branch):
        github = self.getGithubClient(project_name)

        # Note that we directly use a web request here because if we use the
        # github3.py api directly we need a repository object which needs
        # an unneeded web request during creation.
        url = github.session.build_url('repos', project_name,
                                       'branches', branch)

        resp = github.session.get(url)

        if resp.status_code == 404:
            return None

        return resp.json()

    def getPullUrl(self, project, number):
        return '%s/pull/%s' % (self.getGitwebUrl(project), number)

    def getPull(self, project_name, number, event=None):
        log = get_annotated_logger(self.log, event)
        github = self.getGithubClient(project_name, zuul_event_id=event)
        owner, proj = project_name.split('/')
        for retry in range(5):
            try:
                probj = github.pull_request(owner, proj, number)
                if probj is not None:
                    break
                self.log.warning("Pull request #%s of %s/%s returned None!" % (
                                 number, owner, proj))
            except github3.exceptions.GitHubException:
                self.log.warning(
                    "Failed to get pull request #%s of %s/%s; retrying" %
                    (number, owner, proj))
            time.sleep(1)
        pr = probj.as_dict()
        try:
            pr['files'] = [f.filename for f in probj.files()]
        except github3.exceptions.ServerError as exc:
            # NOTE: For PRs with a lot of lines changed, Github will return
            # an error (HTTP 500) because it can't generate the diff.
            self.log.warning("Failed to get list of files from Github. "
                             "Using empty file list to trigger update "
                             "via the merger: %s", exc)
            pr['files'] = []

        labels = [l['name'] for l in pr['labels']]
        pr['labels'] = labels
        log.debug('Got PR %s#%s', project_name, number)
        return (pr, probj)

    def canMerge(self, change, allow_needs, event=None):
        # NOTE: The mergeable call may get a false (null) while GitHub is
        # calculating if it can merge. The github3.py library will just return
        # that as false. This could lead to false negatives. So don't do this
        # call here and only evaluate branch protection settings. Any merge
        # conflicts which would block merging finally will be detected by
        # the zuul-mergers anyway.

        github = self.getGithubClient(change.project.name, zuul_event_id=event)
        owner, proj = change.project.name.split('/')
        pull = github.pull_request(owner, proj, change.number)

        protection = self._getBranchProtection(
            change.project.name, change.branch, zuul_event_id=event)

        if not self._hasRequiredStatusChecks(allow_needs, protection, pull):
            return False

        required_reviews = protection.get(
            'required_pull_request_reviews')
        if required_reviews:
            if required_reviews.get('require_code_owner_reviews'):
                # we need to process the reviews using code owners
                # TODO(tobiash): not implemented yet
                pass
            else:
                # we need to process the review using access rights
                # TODO(tobiash): not implemented yet
                pass

        return True

    def getPullBySha(self, sha, project_name, event):
        log = get_annotated_logger(self.log, event)

        # Serve from the cache if existing
        cached_pr_numbers = self._sha_pr_cache.get(project_name, sha)
        if len(cached_pr_numbers) > 1:
            raise Exception('Multiple pulls found with head sha %s' % sha)
        if len(cached_pr_numbers) == 1:
            for pr in cached_pr_numbers:
                pr_body, pr_obj = self.getPull(project_name, pr, event)
                return pr_body

        github = self.getGithubClient(project_name, zuul_event_id=event)
        issues = list(github.search_issues(sha))

        log.debug('Got PR on project %s for sha %s', project_name, sha)
        if len(issues) > 1:
            raise Exception('Multiple pulls found with head sha %s' % sha)

        if len(issues) == 0:
            return None

        pr_body, pr_obj = self.getPull(
            project_name, issues.pop().issue.number, event)
        self._sha_pr_cache.update(project_name, pr_body)
        return pr_body

    def getPullReviews(self, pr_obj, project, number, event):
        log = get_annotated_logger(self.log, event)
        # make a list out of the reviews so that we complete our
        # API transaction
        revs = [review.as_dict() for review in pr_obj.reviews()]
        log.debug('Got reviews for PR %s#%s', project, number)

        permissions = {}
        reviews = {}

        for rev in revs:
            user = rev.get('user').get('login')
            review = {
                'by': {
                    'username': user,
                    'email': rev.get('user').get('email'),
                },
                'grantedOn': int(time.mktime(self._ghTimestampToDate(
                                             rev.get('submitted_at')))),
            }

            review['type'] = rev.get('state').lower()
            review['submitted_at'] = rev.get('submitted_at')

            # Get user's rights. A user always has read to leave a review
            review['permission'] = 'read'

            if user in permissions:
                permission = permissions[user]
            else:
                permission = self.getRepoPermission(project.name, user)
                permissions[user] = permission

            if permission == 'write':
                review['permission'] = 'write'
            if permission == 'admin':
                review['permission'] = 'admin'

            if user not in reviews:
                reviews[user] = review
            else:
                # if there are multiple reviews per user, keep the newest
                # note that this breaks the ability to set the 'older-than'
                # option on a review requirement.
                # BUT do not keep the latest if it's a 'commented' type and the
                # previous review was 'approved' or 'changes_requested', as
                # the GitHub model does not change the vote if a comment is
                # added after the fact. THANKS GITHUB!
                if review['grantedOn'] > reviews[user]['grantedOn']:
                    if (review['type'] == 'commented' and reviews[user]['type']
                            in ('approved', 'changes_requested')):
                        log.debug("Discarding comment review %s due to "
                                  "an existing vote %s" % (review,
                                                           reviews[user]))
                        pass
                    else:
                        reviews[user] = review

        return reviews.values()

    def _getBranchProtection(self, project_name: str, branch: str,
                             zuul_event_id=None):
        github = self.getGithubClient(
            project_name, zuul_event_id=zuul_event_id)
        url = github.session.build_url('repos', project_name,
                                       'branches', branch,
                                       'protection')

        headers = {'Accept': 'application/vnd.github.loki-preview+json'}
        resp = github.session.get(url, headers=headers)

        if resp.status_code == 404:
            return {}

        return resp.json()

    def _hasRequiredStatusChecks(self, allow_needs, protection, pull):
        if not protection:
            # There are no protection settings -> ok by definition
            return True

        required_contexts = protection.get(
            'required_status_checks', {}).get('contexts')

        if not required_contexts:
            # There are no required contexts -> ok by definition
            return True

        # Strip allow_needs as we will set this in the gate ourselves
        required_contexts = set(
            [x for x in required_contexts if x not in allow_needs])

        # NOTE(tobiash): We cannot just take the last commit in the list
        # because it is not sorted that the head is the last one in every case.
        # E.g. when doing a re-merge from the target the PR head can be
        # somewhere in the middle of the commit list. Thus we need to search
        # the whole commit list for the PR head commit which has the statuses
        # attached.
        commits = list(pull.commits())
        commit = None
        for c in commits:
            if c.sha == pull.head.sha:
                commit = c
                break

        # Get successful statuses
        successful = set([s.context for s in commit.status().statuses
                          if s.state == 'success'])

        # Required contexts must be a subset of the successful contexts as
        # we allow additional successful status contexts we don't care about.
        return required_contexts.issubset(successful)

    def getUser(self, login, project):
        return GithubUser(login, self, project)

    def getRepoPermission(self, project, login):
        github = self.getGithubClient(project)
        owner, proj = project.split('/')
        # This gets around a missing API call
        # need preview header
        headers = {'Accept': 'application/vnd.github.korra-preview'}

        # Create a repo object
        repository = github.repository(owner, proj)

        if not repository:
            return 'none'

        # Build up a URL
        url = repository._build_url('collaborators', login, 'permission',
                                    base_url=repository._api)
        # Get the data
        perms = repository._get(url, headers=headers)

        self.log.debug("Got repo permissions for %s/%s", owner, proj)

        # no known user, maybe deleted since review?
        if perms.status_code == 404:
            return 'none'

        # get permissions from the data
        return perms.json().get('permission', 'none')

    def commentPull(self, project, pr_number, message, zuul_event_id=None):
        log = get_annotated_logger(self.log, zuul_event_id)
        github = self.getGithubClient(project, zuul_event_id=zuul_event_id)
        owner, proj = project.split('/')
        repository = github.repository(owner, proj)
        pull_request = repository.issue(pr_number)
        pull_request.create_comment(message)
        log.debug("Commented on PR %s/%s#%s", owner, proj, pr_number)

    def mergePull(self, project, pr_number, commit_message='', sha=None,
                  method='merge', zuul_event_id=None):
        log = get_annotated_logger(self.log, zuul_event_id)
        github = self.getGithubClient(project)
        owner, proj = project.split('/')
        pull_request = github.pull_request(owner, proj, pr_number)
        try:
            result = pull_request.merge(commit_message=commit_message, sha=sha,
                                        merge_method=method)
        except github3.exceptions.MethodNotAllowed as e:
            raise MergeFailure('Merge was not successful due to mergeability'
                               ' conflict, original error is %s' % e)

        log.debug("Merged PR %s/%s#%s", owner, proj, pr_number)
        if not result:
            raise Exception('Pull request was not merged')

    def _getCommit(self, repository, sha, retries=5):
        try:
            return repository.commit(sha)
        except github3.exceptions.NotFoundError:
            self.log.warning("Commit %s of project %s returned None",
                             sha, repository.name)
            if retries <= 0:
                raise
            time.sleep(1)
            return self._getCommit(repository, sha, retries - 1)

    def getCommitStatuses(self, project_name, sha, zuul_event_id=None):
        log = get_annotated_logger(self.log, zuul_event_id)
        github = self.getGithubClient(
            project_name, zuul_event_id=zuul_event_id)
        url = github.session.build_url('repos', project_name,
                                       'commits', sha, 'statuses')
        params = {'per_page': 100}
        resp = github.session.get(url, params=params)
        resp.raise_for_status()

        log.debug("Got commit statuses for sha %s on %s", sha, project_name)
        return resp.json()

    def setCommitStatus(self, project, sha, state, url='', description='',
                        context='', zuul_event_id=None):
        log = get_annotated_logger(self.log, zuul_event_id)
        github = self.getGithubClient(project, zuul_event_id=zuul_event_id)
        owner, proj = project.split('/')
        repository = github.repository(owner, proj)
        repository.create_status(sha, state, url, description, context)
        log.debug("Set commit status to %s for sha %s on %s",
                  state, sha, project)

    def reviewPull(self, project, pr_number, sha, review, body,
                   zuul_event_id=None):
        github = self.getGithubClient(project, zuul_event_id=zuul_event_id)
        owner, proj = project.split('/')
        pull_request = github.pull_request(owner, proj, pr_number)
        event = review.replace('-', '_')
        event = event.upper()
        pull_request.create_review(body=body, commit_id=sha, event=event)

    def labelPull(self, project, pr_number, label, zuul_event_id=None):
        log = get_annotated_logger(self.log, zuul_event_id)
        github = self.getGithubClient(project, zuul_event_id=zuul_event_id)
        owner, proj = project.split('/')
        pull_request = github.issue(owner, proj, pr_number)
        pull_request.add_labels(label)
        log.debug("Added label %s to %s#%s", label, proj, pr_number)

    def unlabelPull(self, project, pr_number, label, zuul_event_id=None):
        log = get_annotated_logger(self.log, zuul_event_id)
        github = self.getGithubClient(project, zuul_event_id=zuul_event_id)
        owner, proj = project.split('/')
        pull_request = github.issue(owner, proj, pr_number)
        pull_request.remove_label(label)
        log.debug("Removed label %s from %s#%s", label, proj, pr_number)

    def getPushedFileNames(self, event):
        files = set()
        for c in event.commits:
            for f in c.get('added') + c.get('modified') + c.get('removed'):
                files.add(f)
        return list(files)

    def _ghTimestampToDate(self, timestamp):
        return time.strptime(timestamp, '%Y-%m-%dT%H:%M:%SZ')

    def _get_statuses(self, project, sha, event):
        # A ref can have more than one status from each context,
        # however the API returns them in order, newest first.
        # So we can keep track of which contexts we've already seen
        # and throw out the rest. Our unique key is based on
        # the user and the context, since context is free form and anybody
        # can put whatever they want there. We want to ensure we track it
        # by user, so that we can require/trigger by user too.
        seen = []
        statuses = []
        for status in self.getCommitStatuses(
                project.name, sha, event):
            stuple = _status_as_tuple(status)
            if "%s:%s" % (stuple[0], stuple[1]) not in seen:
                statuses.append("%s:%s:%s" % stuple)
                seen.append("%s:%s" % (stuple[0], stuple[1]))

        return statuses

    def getWebController(self, zuul_web):
        return GithubWebController(zuul_web, self)

    def validateWebConfig(self, config, connections):
        if 'webhook_token' not in self.connection_config:
            raise Exception(
                "webhook_token not found in config for connection %s" %
                self.connection_name)
        return True

    def _clearBranchCache(self, project, log):
        log.debug("Clearing branch cache for %s", project.name)
        for cache in [
                self._project_branch_cache_exclude_unprotected,
                self._project_branch_cache_include_unprotected,
        ]:
            try:
                del cache[project.name]
            except KeyError:
                pass

    def checkBranchCache(self, project, branch, protected, log):
        # If the branch appears in the exclude_unprotected cache but
        # is unprotected, clear the exclude cache.

        # If the branch does not appear in the exclude_unprotected
        # cache but is protected, clear the exclude cache.

        # All branches should always appear in the include_unprotected
        # cache, so we never clear it.

        cache = self._project_branch_cache_exclude_unprotected
        branches = cache.get(project.name, [])
        if (branch in branches) and (not protected):
            log.debug("Clearing protected branch cache for %s",
                      project.name)
            try:
                del cache[project.name]
            except KeyError:
                pass
            return
        if (branch not in branches) and (protected):
            log.debug("Clearing protected branch cache for %s",
                      project.name)
            try:
                del cache[project.name]
            except KeyError:
                pass
            return


class GithubWebController(BaseWebController):

    log = logging.getLogger("zuul.GithubWebController")

    def __init__(self, zuul_web, connection):
        self.connection = connection
        self.zuul_web = zuul_web
        self.token = self.connection.connection_config.get('webhook_token')

    def _validate_signature(self, body, headers):
        try:
            request_signature = headers['x-hub-signature']
        except KeyError:
            raise cherrypy.HTTPError(401, 'X-Hub-Signature header missing.')

        payload_signature = _sign_request(body, self.token)

        self.log.debug("Payload Signature: {0}".format(str(payload_signature)))
        self.log.debug("Request Signature: {0}".format(str(request_signature)))
        if not hmac.compare_digest(
            str(payload_signature), str(request_signature)):
            raise cherrypy.HTTPError(
                401,
                'Request signature does not match calculated payload '
                'signature. Check that secret is correct.')

        return True

    @cherrypy.expose
    @cherrypy.tools.json_out(content_type='application/json; charset=utf-8')
    def payload(self):
        # Note(tobiash): We need to normalize the headers. Otherwise we will
        # have trouble to get them from the dict afterwards.
        # e.g.
        # GitHub: sent: X-GitHub-Event received: X-GitHub-Event
        # urllib: sent: X-GitHub-Event received: X-Github-Event
        #
        # We cannot easily solve this mismatch as every http processing lib
        # modifies the header casing in its own way and by specification http
        # headers are case insensitive so just lowercase all so we don't have
        # to take care later.
        # Note(corvus): Don't use cherrypy's json_in here so that we
        # can validate the signature.
        headers = dict()
        for key, value in cherrypy.request.headers.items():
            headers[key.lower()] = value
        body = cherrypy.request.body.read()
        self._validate_signature(body, headers)
        # We cannot send the raw body through gearman, so it's easy to just
        # encode it as json, after decoding it as utf-8
        json_body = json.loads(body.decode('utf-8'))

        job = self.zuul_web.rpc.submitJob(
            'github:%s:payload' % self.connection.connection_name,
            {'headers': headers, 'body': json_body})

        return json.loads(job.data[0])


def _status_as_tuple(status):
    """Translate a status into a tuple of user, context, state"""

    creator = status.get('creator')
    if not creator:
        user = "Unknown"
    else:
        user = creator.get('login')
    context = status.get('context')
    state = status.get('state')
    return (user, context, state)

#!/usr/bin/python3

import argparse
import datetime
import glob
import json
import logging
import os.path
import time

import osc
from osc.core import http_GET, makeurl

from osclib.core import target_archs
from lxml import etree as ET

from urllib.error import HTTPError
from osclib.PubSubConsumer import PubSubConsumer

class Listener(PubSubConsumer):
    def __init__(self, apiurl, amqp_prefix, namespaces):
        super(Listener, self).__init__(amqp_prefix, logging.getLogger(__name__))
        self.apiurl = apiurl
        self.amqp_prefix = amqp_prefix
        self.namespaces = namespaces
        self.repositories_to_check = []

    def interval(self):
        if len(self.repositories_to_check):
            return 5
        return 300

    def still_alive(self):
        self.check_some_repos()
        super(Listener, self).still_alive()

    def routing_keys(self):
        return [self.amqp_prefix + '.obs.repo.build_finished']

    def check_arch(self, project, repository, architecture):
        url = makeurl(self.apiurl, [
                      'build', project, repository, architecture], {'view': 'status'})
        root = ET.parse(http_GET(url)).getroot()
        if root.get('code') == 'finished':
            buildid = root.find('buildid')
            if buildid is not None:
                return buildid.text

    def check_all_archs(self, project, repository):
        ids = {}
        try:
            archs = target_archs(self.apiurl, project, repository)
        except HTTPError:
            return None
        for arch in archs:
            repoid = self.check_arch(project, repository, arch)
            if not repoid:
                self.logger.info('{}/{}/{} not yet done'.format(project, repository, arch))
                return None
            ids[arch] = repoid
        self.logger.info('All of {}/{} finished'.format(project, repository))
        return ids

    def is_part_of_namespaces(self, project):
        for namespace in self.namespaces:
            if project.startswith(namespace):
                return True

    def start_consuming(self):
        # now we are (re-)connected to the bus and need to fetch the
        # initial state
        for namespace in self.namespaces:
            for state in glob.glob('{}*.yaml'.format(namespace)):
                state = state.replace('.yaml', '')
                # split
                project, repository = state.split('_-_')
                self.repositories_to_check.append([project, repository])
        self.check_some_repos()
        super(Listener, self).start_consuming()

    def check_some_repos(self):
        count = 0
        limit = 25
        while len(self.repositories_to_check):
            project, repository = self.repositories_to_check.pop()
            self.logger.debug(f"Check repo {project}/{repository}")
            count += 1
            if count >= limit:
                return

    def push_git(self, message):
        os.system('git add . ')
        os.system('git commit -m "{}" > /dev/null'.format(message))
        os.system('git push > /dev/null')

    def update_repo(self, project, repository):
        ids = self.check_all_archs(project, repository)
        if not ids:
            return
        pathname = project + '_-_' + repository + '.yaml'
        with open(pathname, 'w') as f:
            for arch in sorted(ids.keys()):
                f.write('{}: {}\n'.format(arch, ids[arch]))
        self.push_git('Repository update: {}/{}'.format(project, repository))

    def on_message(self, unused_channel, method, properties, body):
        self.logger.debug("on_message")
        self.check_some_repos()
        self.acknowledge_message(method.delivery_tag)
        try:
            body = json.loads(body)
        except ValueError:
            return
        if method.routing_key.endswith('.obs.repo.build_finished'):
            if not self.is_part_of_namespaces(body['project']):
                return
            self.restart_timer()
            self.logger.info('Repo finished event: {}/{}/{}'.format(body['project'], body['repo'], body['arch']))
            self.update_repo(body['project'], body['repo'])
        else:
            self.logger.warning(
                'unknown rabbitmq message {}'.format(method.routing_key))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Monitor to commit repo status to git (for gocd trigger)')
    parser.add_argument('--apiurl', '-A', type=str, help='API URL of OBS')
    parser.add_argument('-d', '--debug', action='store_true', default=False,
                        help='enable debug information')
    parser.add_argument('namespaces', nargs='*', help='namespaces to wait for')

    args = parser.parse_args()

    osc.conf.get_config(override_apiurl=args.apiurl)
    osc.conf.config['debug'] = args.debug

    apiurl = osc.conf.config['apiurl']

    if apiurl.endswith('suse.de'):
        amqp_prefix = 'suse'
    else:
        amqp_prefix = 'opensuse'

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)

    listener = Listener(apiurl, amqp_prefix, args.namespaces)

    try:
        listener.run(108000)
    except KeyboardInterrupt:
        listener.stop()

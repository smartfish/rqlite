#!/usr/bin/env python

from __future__ import print_function
import tempfile
import argparse
import subprocess
import requests
import json
import os
import shutil
import time
import socket
import sys
from urlparse import urlparse
import unittest

RQLITED_PATH = os.environ['RQLITED_PATH']

class Node(object):
  def __init__(self, path, node_id, api_addr=None, raft_addr=None, dir=None):
    if api_addr is None:
      api_addr = self._random_addr()
    if raft_addr is None:
      raft_addr = self._random_addr()
    if dir is None:
      dir = tempfile.mkdtemp()

    self.path = path
    self.node_id = node_id
    self.api_addr = api_addr
    self.raft_addr = raft_addr
    self.dir = dir
    self.process = None
    self.stdout_file = os.path.join(dir, 'rqlited.log')
    self.stdout_fd = open(self.stdout_file, 'w')
    self.stderr_file = os.path.join(dir, 'rqlited.err')
    self.stderr_fd = open(self.stderr_file, 'w')

  def scramble_network(self):
    self.api_addr = self._random_addr()
    self.raft_addr = self._random_addr()

  def start(self, join=None, wait=True, timeout=30):
    if self.process is not None:
      return
    command = [self.path,
               '-node-id', self.node_id,
               '-http-addr', self.api_addr,
               '-raft-addr', self.raft_addr]
    if join is not None:
      command += ['-join', 'http://' + join]
    command.append(self.dir)
    self.process = subprocess.Popen(command, stdout=self.stdout_fd, stderr=self.stderr_fd)
    t = 0
    while wait:
      if t > timeout:
        raise Exception('timeout')
      try:
        self.status()
      except requests.exceptions.ConnectionError:
        time.sleep(1)
        t+=1
      else:
        break
    return self

  def stop(self):
    if self.process is None:
      return
    self.process.kill()
    self.process.wait()
    self.process = None
    return self

  def status(self):
    r = requests.get(self._status_url())
    r.raise_for_status()
    return r.json()

  def is_leader(self):
    try:
      return self.status()['store']['raft']['state'] == 'Leader'
    except requests.exceptions.ConnectionError:
      return False

  def is_follower(self):
    try:
      return self.status()['store']['raft']['state'] == 'Follower'
    except requests.exceptions.ConnectionError:
      return False

  def wait_for_leader(self, timeout=30):
    l = None
    t = 0
    while l == None or l is '':
      if t > timeout:
        raise Exception('timeout')
      l = self.status()['store']['leader']
      time.sleep(1)
      t+=1
    return l

  def applied_index(self):
    return self.status()['store']['raft']['applied_index']

  def wait_for_applied_index(self, index, timeout=30):
    t = 0
    while self.status()['store']['raft']['applied_index'] != index:
      if t > timeout:
        raise Exception('timeout')
      time.sleep(1)
      t+=1

  def query(self, statement, level='weak'):
    r = requests.get(self._query_url(), params={'q': statement, 'level': level})
    r.raise_for_status()
    return r.json()

  def execute(self, statement):
    r = requests.post(self._execute_url(), data=json.dumps([statement]))
    r.raise_for_status()
    return r.json()

  def redirect_addr(self):
    r = requests.post(self._execute_url(), data=json.dumps(['nonsense']), allow_redirects=False)
    if r.status_code == 301:
      return urlparse(r.headers['Location']).netloc

  def _status_url(self):
    return 'http://' + self.api_addr + '/status'
  def _query_url(self):
    return 'http://' + self.api_addr + '/db/query'
  def _execute_url(self):
    return 'http://' + self.api_addr + '/db/execute'
  def _random_addr(self):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('localhost', 0))
    return ':'.join([s.getsockname()[0], str(s.getsockname()[1])])

  def __eq__(self, other):
    return self.node_id == other.node_id
  def __str__(self):
    return '%s:[%s]:[%s]:[%s]' % (self.node_id, self.api_addr, self.raft_addr, self.dir)
  def __del__(self):
    self.stdout_fd.close()
    self.stderr_fd.close()

def deprovision_node(node):
  node.stop()
  shutil.rmtree(node.dir)
  node = None

class Cluster(object):
  def __init__(self, nodes):
    self.nodes = nodes
  def wait_for_leader(self, node_exc=None, timeout=30):
    t = 0
    while True:
      if t > timeout:
        raise Exception('timeout')
      for n in self.nodes:
        if node_exc is not None and n == node_exc:
          continue
        if n.is_leader():
          return n
      time.sleep(1)
      t+=1
  def followers(self):
    return [n for n in self.nodes if n.is_follower()]
  def deprovision(self):
    for n in self.nodes:
      deprovision_node(n)

class TestEndToEnd(unittest.TestCase):
  def setUp(self):
    n0 = Node(RQLITED_PATH, '0')
    n0.start()
    n0.wait_for_leader()

    n1 = Node(RQLITED_PATH, '1')
    n1.start(join=n0.api_addr)
    n1.wait_for_leader()

    n2 = Node(RQLITED_PATH, '2')
    n2.start(join=n0.api_addr)
    n2.wait_for_leader()

    self.cluster = Cluster([n0, n1, n2])

  def tearDown(self):
    self.cluster.deprovision()

  def test_election(self):
    '''Test basic leader election'''

    n = self.cluster.wait_for_leader().stop()
    self.cluster.wait_for_leader(node_exc=n)
    n.start()
    n.wait_for_leader()

  def test_execute_fail_rejoin(self):
    '''Test that a node that fails can rejoin the cluster, and picks up changes'''

    n = self.cluster.wait_for_leader()
    j = n.execute('CREATE TABLE foo (id INTEGER NOT NULL PRIMARY KEY, name TEXT)')
    self.assertEqual(str(j), "{u'results': [{}]}")
    j = n.execute('INSERT INTO foo(name) VALUES("fiona")')
    self.assertEqual(str(j), "{u'results': [{u'last_insert_id': 1, u'rows_affected': 1}]}")
    j = n.query('SELECT * FROM foo')
    self.assertEqual(str(j), "{u'results': [{u'values': [[1, u'fiona']], u'types': [u'integer', u'text'], u'columns': [u'id', u'name']}]}")

    n0 = self.cluster.wait_for_leader().stop()
    n1 = self.cluster.wait_for_leader(node_exc=n0)
    j = n1.query('SELECT * FROM foo')
    self.assertEqual(str(j), "{u'results': [{u'values': [[1, u'fiona']], u'types': [u'integer', u'text'], u'columns': [u'id', u'name']}]}")
    j = n1.execute('INSERT INTO foo(name) VALUES("declan")')
    self.assertEqual(str(j), "{u'results': [{u'last_insert_id': 2, u'rows_affected': 1}]}")

    n0.start()
    n0.wait_for_leader()
    n0.wait_for_applied_index(n1.applied_index())
    j = n0.query('SELECT * FROM foo', level='none')
    self.assertEqual(str(j), "{u'results': [{u'values': [[1, u'fiona'], [2, u'declan']], u'types': [u'integer', u'text'], u'columns': [u'id', u'name']}]}")

  def test_leader_redirect(self):
    '''Test that followers supply the correct leader redirects (HTTP 301)'''

    l = self.cluster.wait_for_leader()
    fs = self.cluster.followers()
    self.assertEqual(len(fs), 2)
    for n in fs:
      self.assertEqual(l.api_addr, n.redirect_addr())

    l.stop()
    n = self.cluster.wait_for_leader(node_exc=l)
    for f in self.cluster.followers():
      self.assertEqual(n.api_addr, f.redirect_addr())

  def test_node_restart_different_ip(self):
    ''' Test that a node restarting with different IP addresses successfully rejoins the cluster'''

    self.cluster.wait_for_leader()
    f = self.cluster.followers()[0]
    f.stop()
    l = self.cluster.wait_for_leader()
    j = l.execute('CREATE TABLE foo (id INTEGER NOT NULL PRIMARY KEY, name TEXT)')

    self.assertEqual(str(j), "{u'results': [{}]}")
    j = l.execute('INSERT INTO foo (name) VALUES("fiona")')
    self.assertEqual(str(j), "{u'results': [{u'last_insert_id': 1, u'rows_affected': 1}]}")

    f.scramble_network()
    f.start(join=l.api_addr)
    f.wait_for_leader()
    f.wait_for_applied_index(l.applied_index())
    j = f.query('SELECT * FROM foo', level='none')
    self.assertEqual(str(j), "{u'results': [{u'values': [[1, u'fiona']], u'types': [u'integer', u'text'], u'columns': [u'id', u'name']}]}")

if __name__ == "__main__":
  unittest.main(verbosity=2)

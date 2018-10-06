# Copyright: 2006 Brian Harring <ferringb@gmail.com>
# License: GPL2/BSD

import os
import pwd

from pkgcore.sync import base, svn
from tests.sync.syncer import make_bogus_syncer, make_valid_syncer
from snakeoil.test import TestCase, SkipTest

valid = make_valid_syncer(base.ExternalSyncer)
bogus = make_bogus_syncer(base.ExternalSyncer)

existing_user = pwd.getpwall()[0].pw_name
existing_uid = pwd.getpwnam(existing_user).pw_uid


class TestBase(TestCase):

    def test_init(self):
        self.assertRaises(
            base.syncer_exception, bogus,
            "/tmp/foon", "http://dar")
        o = valid("/tmp/foon", "http://dar")
        self.assertEqual(o.local_user, os.getuid())
        self.assertEqual(o.uri, "http://dar")

        o = valid("/tmp/foon", f"http://{existing_user}::@site")
        self.assertEqual(o.local_user, existing_uid)
        self.assertEqual(o.uri, "http://site")

        o = valid("/tmp/foon", f"http://{existing_user}::foon@site")
        self.assertEqual(o.local_user, existing_uid)
        self.assertEqual(o.uri, "http://foon@site")

        o = valid("/tmp/foon", f"{existing_user}::foon@site")
        self.assertEqual(o.local_user, existing_uid)
        self.assertEqual(o.uri, "foon@site")


class GenericSyncerTest(TestCase):

    def test_init(self):
        self.assertRaises(
            base.uri_exception,
            base.GenericSyncer, '/', 'seriouslynotaprotocol://blah/')
        # TODO this should be using a syncer we know is always available.
        try:
            syncer = base.GenericSyncer('/', 'svn://blah/')
        except base.uri_exception as e:
            if str(e) == "no known syncer supports 'svn://blah/'":
                raise SkipTest('svn syncer unavailable')
            raise
        self.assertIdentical(svn.svn_syncer, syncer.__class__)

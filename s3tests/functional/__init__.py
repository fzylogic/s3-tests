import ConfigParser
import boto.exception
import boto.s3.connection
import bunch
import itertools
import os
import random
import string

from .utils import region_sync_meta

s3 = bunch.Bunch()
config = bunch.Bunch()
targets = bunch.Bunch()

# this will be assigned by setup()
prefix = None

calling_formats = dict(
    ordinary=boto.s3.connection.OrdinaryCallingFormat(),
    subdomain=boto.s3.connection.SubdomainCallingFormat(),
    vhost=boto.s3.connection.VHostCallingFormat(),
    )

def get_prefix():
    assert prefix is not None
    return prefix

def is_slow_backend():
    return slow_backend

def choose_bucket_prefix(template, max_len=30):
    """
    Choose a prefix for our test buckets, so they're easy to identify.

    Use template and feed it more and more random filler, until it's
    as long as possible but still below max_len.
    """
    rand = ''.join(
        random.choice(string.ascii_lowercase + string.digits)
        for c in range(255)
        )

    while rand:
        s = template.format(random=rand)
        if len(s) <= max_len:
            return s
        rand = rand[:-1]

    raise RuntimeError(
        'Bucket prefix template is impossible to fulfill: {template!r}'.format(
            template=template,
            ),
        )


def nuke_prefixed_buckets_on_conn(prefix, name, conn):
    print 'Cleaning buckets from connection {name} prefix {prefix!r}.'.format(
        name=name,
        prefix=prefix,
        )
    for bucket in conn.get_all_buckets():
        if bucket.name.startswith(prefix):
            print 'Cleaning bucket {bucket}'.format(bucket=bucket)
            try:
                bucket.set_canned_acl('private')
                for key in bucket.list():
                    print 'Cleaning bucket {bucket} key {key}'.format(
                        bucket=bucket,
                        key=key,
                        )
                    key.set_canned_acl('private')
                    key.delete()
                bucket.delete()
            except boto.exception.S3ResponseError as e:
                if e.error_code != 'AccessDenied':
                    print 'GOT UNWANTED ERROR', e.error_code
                    raise
                # seems like we're not the owner of the bucket; ignore
                pass

def nuke_prefixed_buckets(prefix):
    # If no regions are specified, use the simple method
    if targets.main.master == None:
        for name, conn in s3.items():
            print 'Deleting buckets on {name}'.format(name=name)
            nuke_prefixed_buckets_on_conn(prefix, name, conn)
    else: 
		    # First, delete all buckets on the master connection 
		    for name, conn in s3.items():
		        if conn == targets.main.master.connection:
		            print 'Deleting buckets on {name} (master)'.format(name=name)
		            nuke_prefixed_buckets_on_conn(prefix, name, conn)
		
		    # Then sync to propagate deletes to secondaries
		    region_sync_meta(targets.main, targets.main.master.connection)
		    print 'region-sync in nuke_prefixed_buckets'
		
		    # Now delete remaining buckets on any other connection 
		    for name, conn in s3.items():
		        if conn != targets.main.master.connection:
		            print 'Deleting buckets on {name} (non-master)'.format(name=name)
		            nuke_prefixed_buckets_on_conn(prefix, name, conn)

    print 'Done with cleanup of test buckets.'


class TargetConfig:
    def __init__(self, cfg, section):
        self.port = None
        self.api_name = ''
        self.is_master = False
        self.is_secure = False
        self.sync_agent_addr = None
        self.sync_agent_port = 0
        self.sync_meta_wait = 0
        try:
            self.api_name = cfg.get(section, 'api_name')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            pass
        try:
            self.port = cfg.getint(section, 'port')
        except ConfigParser.NoOptionError:
            pass
        try:
            self.host=cfg.get(section, 'host')
        except ConfigParser.NoOptionError:
            raise RuntimeError(
                'host not specified for section {s}'.format(s=section)
                )
        try:
            self.is_master=cfg.getboolean(section, 'is_master')
        except ConfigParser.NoOptionError:
            pass

        try:
            self.is_secure=cfg.getboolean(section, 'is_secure')
        except ConfigParser.NoOptionError:
            pass

        try:
            raw_calling_format = cfg.get(section, 'calling_format')
        except ConfigParser.NoOptionError:
            raw_calling_format = 'ordinary'

        try:
            self.sync_agent_addr = cfg.get(section, 'sync_agent_addr')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            pass

        try:
            self.sync_agent_port = cfg.getint(section, 'sync_agent_port')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            pass

        try:
            self.sync_meta_wait = cfg.getint(section, 'sync_meta_wait')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            pass


        try:
            self.calling_format = calling_formats[raw_calling_format]
        except KeyError:
            raise RuntimeError(
                'calling_format unknown: %r' % raw_calling_format
                )

class TargetConnection:
    def __init__(self, conf, conn):
        self.conf = conf
        self.connection = conn



class RegionsInfo:
    def __init__(self):
        self.m = bunch.Bunch()
        self.master = None
        self.secondaries = []

    def add(self, name, region_config):
        self.m[name] = region_config
        if (region_config.is_master):
            if not self.master is None:
                raise RuntimeError(
                    'multiple regions defined as master'
                    )
            self.master = region_config
        else:
            self.secondaries.append(region_config)
    def get(self, name):
        return self.m[name]
    def get(self):
        return self.m
    def iteritems(self):
        return self.m.iteritems()

regions = RegionsInfo()


class RegionsConn:
    def __init__(self):
        self.m = bunch.Bunch()
        self.default = None
        self.master = None
        self.secondaries = []

    def iteritems(self):
        return self.m.iteritems()

    def set_default(self, conn):
        self.default = conn

    def add(self, name, conn):
        self.m[name] = conn
        if not self.default:
            self.default = conn
        if (conn.conf.is_master):
            self.master = conn
        else:
            self.secondaries.append(conn)


# nosetests --processes=N with N>1 is safe
_multiprocess_can_split_ = True

def setup():

    cfg = ConfigParser.RawConfigParser()
    try:
        path = os.environ['S3TEST_CONF']
    except KeyError:
        raise RuntimeError(
            'To run tests, point environment '
            + 'variable S3TEST_CONF to a config file.',
            )
    with file(path) as f:
        cfg.readfp(f)

    global prefix
    global targets
    global slow_backend

    try:
        template = cfg.get('fixtures', 'bucket prefix')
    except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
        template = 'test-{random}-'
    prefix = choose_bucket_prefix(template=template)

    try:
        slow_backend = cfg.getboolean('fixtures', 'slow backend')
    except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
        slow_backend = False

    # pull the default_region out, if it exists
    try:
        default_region = cfg.get('fixtures', 'default_region')
    except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
        default_region = None

    s3.clear()
    config.clear()

    for section in cfg.sections():
        try:
            (type_, name) = section.split(None, 1)
        except ValueError:
            continue
        if type_ != 'region':
            continue
        regions.add(name, TargetConfig(cfg, section))

    for section in cfg.sections():
        try:
            (type_, name) = section.split(None, 1)
        except ValueError:
            continue
        if type_ != 's3':
            continue

        if len(regions.get()) == 0:
            regions.add("default", TargetConfig(cfg, section))

        config[name] = bunch.Bunch()
        for var in [
            'user_id',
            'display_name',
            'email',
            ]:
            try:
                config[name][var] = cfg.get(section, var)
            except ConfigParser.NoOptionError:
                pass

        targets[name] = RegionsConn()

        for (k, conf) in regions.iteritems():
            conn = boto.s3.connection.S3Connection(
                aws_access_key_id=cfg.get(section, 'access_key'),
                aws_secret_access_key=cfg.get(section, 'secret_key'),
                is_secure=conf.is_secure,
                port=conf.port,
                host=conf.host,
                # TODO test vhost calling format
                calling_format=conf.calling_format,
                )

            temp_targetConn = TargetConnection(conf, conn)
            targets[name].add(k, temp_targetConn)

            # Explicitly test for and set the default region, if specified.
            # If it was not specified, use the 'is_master' flag to set it.
            if default_region:
                if default_region == name:
                    targets[name].set_default(temp_targetConn)
            elif conf.is_master:
                targets[name].set_default(temp_targetConn)

        s3[name] = targets[name].default.connection

    # WARNING! we actively delete all buckets we see with the prefix
    # we've chosen! Choose your prefix with care, and don't reuse
    # credentials!

    # We also assume nobody else is going to use buckets with that
    # prefix. This is racy but given enough randomness, should not
    # really fail.
    nuke_prefixed_buckets(prefix=prefix)


def teardown():
    # remove our buckets here also, to avoid littering
    nuke_prefixed_buckets(prefix=prefix)


bucket_counter = itertools.count(1)


def get_new_bucket_name():
    """
    Get a bucket name that probably does not exist.

    We make every attempt to use a unique random prefix, so if a
    bucket by this name happens to exist, it's ok if tests give
    false negatives.
    """
    name = '{prefix}{num}'.format(
        prefix=prefix,
        num=next(bucket_counter),
        )
    return name


def get_new_bucket(target=None, name=None, headers=None):
    """
    Get a bucket that exists and is empty.

    Always recreates a bucket from scratch. This is useful to also
    reset ACLs and such.
    """
    if target is None:
        target = targets.main.default
    connection = target.connection
    if name is None:
        name = get_new_bucket_name()
    # the only way for this to fail with a pre-existing bucket is if
    # someone raced us between setup nuke_prefixed_buckets and here;
    # ignore that as astronomically unlikely
    bucket = connection.create_bucket(name, location=target.conf.api_name, headers=headers)
    return bucket

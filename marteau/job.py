import random
import os
from StringIO import StringIO
import sys
import argparse
import logging
import tempfile
import time
import urllib

try:
    from gevent.subprocess import Popen, PIPE
except ImportError:
    from gevent_subprocess import Popen, PIPE

import gevent

from marteau import __version__, logger
from marteau.queue import Queue
from marteau.config import read_yaml_config
from marteau.redirector import Redirector
from marteau.util import send_report, configure_logger, import_string
from marteau.fixtures import get_fixture
from marteau.aws import AWSConnection

from macauthlib import sign_request
from webob import Request
from wsgiproxy.exactproxy import proxy_exact_request
import tokenlib


DEFAULT_WORKDIR = '/tmp'
DEFAULT_REPORTSDIR = '/tmp'


LOG_LEVELS = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG}

LOG_FMT = r"%(asctime)s [%(process)d] [%(levelname)s] %(message)s"
LOG_DATE_FMT = r"%Y-%m-%d %H:%M:%S"
CSS_FILE = os.path.join(os.path.dirname(__file__), 'media', 'marteau.css')


class RedisIO(StringIO):
    def __init__(self, orig, pid=None):
        StringIO.__init__(self)
        self.orig = orig
        self._queue = Queue()
        if pid is None:
            self.pid = os.getpid()
        else:
            self.pid = pid

    def write(self, msg):
        self.orig.write(msg)
        job_id = self._queue.pid_to_jobid(self.pid)
        if job_id is not None:
            self._queue.append_console(job_id, msg)


def _logrun(msg, eol=True):
    if eol:
        msg += '\n'
    sys.stderr.write(msg)
    sys.stderr.flush()


def _stream(data):
    _logrun(data['data'], eol=False)


def run_func(queue, job_id, cmd, stop_on_failure=True):
    redirector = Redirector(_stream)
    _logrun(cmd)

    try:
        process = Popen(cmd, shell=True, stdout=PIPE, stderr=PIPE,
                        close_fds=True)
        redirector.add_redirection('marteau-stdout', process, process.stdout)
        redirector.add_redirection('marteau-stderr', process, process.stderr)
        redirector.start()
        pid = process.pid
        queue.add_pid(job_id, pid)
        process.wait()
        res = process.returncode
        if res != 0 and stop_on_failure:
            _logrun("%r failed" % cmd)
            raise Exception("%r failed" % cmd)
        return res
    finally:
        redirector.kill()
        queue.remove_pid(job_id, pid)


run_bench = "%s -c 'from funkload.BenchRunner import main; main()'"
run_bench = run_bench % sys.executable
run_report = "%s -c 'from funkload.ReportBuilder import main; main()'"
run_report = run_report % sys.executable
run_pip = "%s -c 'from pip import runner; runner.run()'"
run_pip = run_pip % sys.executable


def catch_std(func):
    def _std(*args, **kwargs):
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = RedisIO(sys.stdout)
        sys.stderr = RedisIO(sys.stderr)
        try:
            return func(*args, **kwargs)
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
    return _std


def cleanup(func):
    def _cleanup(*args, **kwargs):
        _queue = Queue()
        kwargs['queue'] = _queue
        try:
            return func(*args, **kwargs)
        finally:
            job_id = os.environ.get('MARTEAU_JOBID')
            if job_id is not None:
                _queue.delete_pids(job_id)
    return _cleanup


_AWSCON = None


def _get_aws(options):
    global _AWSCON

    if _AWSCON is None:
        access_key = options['aws.access_key']
        secret_key = options['aws.secret_key']
        region = options.get('aws.region', 'us-west-2')
        _AWSCON = AWSConnection(access_key, secret_key, region)

    return _AWSCON


def release_nodes(nodes, queue, options):
    if options.get('aws', False):
        _logrun('Releasing Nodes on AWS')
        conn = _get_aws(options)
        conn.terminate_nodes(nodes)
    else:
        for node in nodes:
            node.status = 'working'
            queue.save_node(node)


def get_nodes(nodes_count, queue, options):
    if options.get('aws', False):
        key_name = options['aws.key_name']
        image_id = options['aws.image_id']
        instance_type = options.get('aws.instance_type', 't1.micro')
        sec = options.get('security_groups', 'marteau')
        conn = _get_aws(options)
        _logrun('Provisioning Nodes on AWS')
        nodes = conn.create_nodes(image_id, nodes_count, instance_type,
                                  security_groups=[sec], key_name=key_name)
        _logrun('Sleeping for 30 s.')
        time.sleep(30.)
    else:
        # we want to pick up the number of nodes asked
        nodes = queue.get_nodes(check_available=True)

        if len(nodes) < nodes_count:
            # we want to pile this one back and sleep a bit here
            _logrun('Sleeping for 30 s.')
            time.sleep(30)
            raise ValueError("Sorry could not find enough free nodes")

        # then pick random ones
        random.shuffle(nodes)
        nodes = nodes[:nodes_count]

        # save the nodes status
        for node in nodes:
            node.status = 'working'
            queue.save_node(node)

    node_user = options.get('node_user')
    if node_user is not None:
        for node in nodes:
            node.name = '%s@%s' % (node_user, node.name)

    return nodes


def _rt_handler(msg, **kw):
    return
    # XXX
    if msg['result'] == 'failure':
        msg = 'F'
    else:
        msg = '.'
    queue = kw['queue']
    job_id = queue.pid_to_jobid(kw['pid'])
    if job_id is not None:
        queue.append_console(job_id, msg)


@catch_std
@cleanup
def run_loadtest(repo, cycles=None, nodes_count=None, duration=None,
                 email=None, options=None, distributed=True,
                 fl_result_path=None, queue=None, fixture_plugin=None,
                 fixture_options=None, workdir=DEFAULT_WORKDIR,
                 reportsdir=DEFAULT_REPORTSDIR, test=None, script=None):
    if options is None:
        options = {}

    rtfeedback = options.get('feedback', None) is not None

    # loading the fixtures plugins
    for fixture in options.get('fixtures', []):
        import_string(fixture)

    job_id = os.environ.get('MARTEAU_JOBID', '')

    if options is None:
        options = {}

    if os.path.exists(repo):
        # just a local dir, lets work there
        os.chdir(repo)
        _logrun('Moved to %r' % repo)
        target = os.path.realpath(repo)
    else:
        # checking out the repo
        os.chdir(workdir)
        name = repo.split('/')[-1].split('.')[0]
        target = os.path.join(workdir, name)
        if os.path.exists(target):
            os.chdir(target)
            _logrun('Moved to %r' % target)
            run_func(queue, job_id, 'git pull')
        else:
            _logrun('Moved to %r' % workdir)
            run_func(queue, job_id,
                     'git clone %s' % repo, stop_on_failure=False)
            os.chdir(target)

    # now looking for the marteau config file in there
    config = read_yaml_config(os.getcwd())

    wdir = config.get('wdir')
    if wdir is not None:
        target = os.path.join(target, wdir)
        os.chdir(target)

    deps = config.get('deps', [])
    if rtfeedback and 'pyzmq' not in deps:
        deps.append('pyzmq')

    if distributed:
        # is this a distributed test ?
        if nodes_count in (None, ''):    # XXX fix later
            nodes_count = config.get('nodes', 1)
        else:
            nodes_count = int(nodes_count)

        nodes = get_nodes(nodes_count, queue, options)
        nodes_names = ','.join([node.name for node in nodes])
        os.environ['MARTEAU_NODES'] = nodes_names
        workers = '--distribute-workers=%s' % nodes_names
        cmd = '%s --distribute %s' % (run_bench, workers)
        if deps != []:
            cmd += ' --distributed-packages="%s"' % ' '.join(deps)
        target = tempfile.mkdtemp()
        cmd += ' --distributed-log-path=%s' % target
        if 'ssh_key' in options:
            cmd += ' --distributed-key-filename=%s' % options['ssh_key']

        # asking the node to send us realtime feedback.
        if rtfeedback:
            cmd += ' --feedback'
            cmd += ' --feedback-endpoint %s' % options['feedback_endpoint']
            cmd += ' --feedback-pubsub-endpoint %s' % \
                        options['feedback_publisher']

    else:
        cmd = run_bench

    try:
        # creating a virtualenv there
        run_func(queue, job_id, 'virtualenv --no-site-packages .')
        run_func(queue, job_id, run_pip + ' install funkload')

        # install dependencies if any
        for dep in deps:
            run_func(queue, job_id, run_pip + ' install %s' % dep)

        if fl_result_path is not None:
            # in funkload this is a relative path
            target = os.path.join(target, fl_result_path)
        xml_files = os.path.join(target, '*.xml')

        if cycles is None:
            cycles = config.get('cycles')

        if cycles is not None:
            cmd += ' --cycles=%s' % cycles

        if duration is None:
            duration = config.get('duration')

        if test is None:
            test = config.get('test')

        if script is None:
            script = config.get('script')

        if duration is not None:
            cmd += ' --duration=%s' % duration

        report_dir = os.path.join(reportsdir,
                                os.environ.get('MARTEAU_JOBID', 'report'))

        if fixture_plugin:
            _logrun('Running the %r fixture' % fixture_plugin)
            fixture_klass = get_fixture(fixture_plugin)
            if fixture_options is None:
                fixture_options = {}
            try:
                fixture = fixture_klass(**fixture_options)
            except:
                _logrun('Could not instanciate a fixture plugin instance')
                raise

            try:
                fixture.setup()
            except:
                _logrun('The fixture set up failed')
                raise

        # starting the feedback subscriberin its own thread
        if rtfeedback:
            from funkload.rtfeedback import FeedbackSubscriber
            sub = FeedbackSubscriber(pubsub_endpoint=options['feedback_publisher'],
                                     handler=_rt_handler,
                                     pid=os.getpid(), queue=queue)
            sub.start()

        try:
            _logrun('Running the loadtest')
            run_func(queue, job_id, '%s %s %s' % (cmd, script, test))
        finally:
            _logrun('Running the fixture tear_down method')
            if fixture_plugin:
                try:
                    fixture.tear_down()
                except:
                    _logrun('The fixture tear down failed')
                    raise

            if rtfeedback:
                sub.terminate()

        _logrun('Building the report')
        report = run_report + ' --skip-definitions --css %s --html -r %s  %s'
        run_func(queue, job_id, report % (CSS_FILE, report_dir, xml_files))

        # do we send an email with the result ?
        if email is None:
            email = config.get('email')

        if email is not None:
            _logrun('Sending an e-mail to %r' % email)
            try:
                res, msg = send_report(email, job_id, **options)
            except Exception, e:
                res = False
                msg = str(e)

            if not res:
                _logrun(msg)
            else:
                _logrun('Mail sent.')

        return report_dir
    finally:
        if distributed:
            release_nodes(nodes, queue, options)


def send_job(repo, server, cycles='', duration='', nodes='', redirect_url=''):
    mac_user = os.environ.get('MACAUTH_USER')
    mac_secret = os.environ.get('MACAUTH_SECRET')
    request = Request.blank(server.rstrip('/') + '/test')
    request.method = 'POST'
    params = {'repo': repo, 'cycles': cycles, 'duration': duration,
              'nodes': nodes, 'redirect_url': redirect_url,
              'api_call': 1}

    request.body = urllib.urlencode(params)
    request.environ['CONTENT_TYPE'] = 'application/x-www-form-urlencoded'

    if mac_user is not None:
        tokenid = tokenlib.make_token({"user": mac_user}, secret=mac_secret)
        key = tokenlib.get_token_secret(tokenid, secret=mac_secret)
        sign_request(request, tokenid, key)

    resp = request.get_response(proxy_exact_request)
    if resp.status_int == 401:
        raise ValueError("Authorization Failed!")

    job_id = resp.json['job_id']
    return server.rstrip('/') + '/test/' + job_id


def main():
    parser = argparse.ArgumentParser(description='Drives Funkload.')
    parser.add_argument('repo', help='Git repository or local directory',
                        nargs='?')
    parser.add_argument('--version', action='store_true',
                        default=False,
                        help='Displays Marteau version and exits.')
    parser.add_argument('--log-level', dest='loglevel', default='info',
                        choices=LOG_LEVELS.keys() + [key.upper() for key in
                                                     LOG_LEVELS.keys()],
                        help="log level")
    parser.add_argument('--log-output', dest='logoutput', default='-',
                        help="log output")
    parser.add_argument('--fl-result-path', dest='result_path', default=None,
                        help="Output path of Funkload result xml files.")
    parser.add_argument('--distributed', action='store_true',
                        default=False,
                        help='Run with the nodes')
    parser.add_argument('--fixture-plugin', default=None,
                        help='The fixture to use for this loadtest.')
    parser.add_argument('--fixture-options', default=None,
                        help='Options to pass to the fixture.')
    parser.add_argument('--server', dest='server',
                        default=None,
                        help="Marteau Server to send the job to")

    args = parser.parse_args()

    if args.version:
        print(__version__)
        sys.exit(0)

    if args.repo is None:
        parser.print_usage()
        sys.exit(0)

    # configure the logger
    configure_logger(logger, args.loglevel, args.logoutput)

    if args.server and os.path.exists(args.repo):
        logging.error("You can't run on a server and provide a local dir!")
        sys.exit(1)

    if args.server:
        logger.info('Sending the job to the Marteau server')
        test = send_job(args.repo, args.server)
        logger.info('Test added at %r' % test)
        logger.info('Bye!')
        sys.exit(0)
    else:
        options = {'feedback': 1,
                   'feedback_endpoint': 'tcp://0.0.0.0:5555',
                   'feedback_publisher': 'tcp://0.0.0.0:5556'}

        logger.info('Hammer ready. Where are the nails ?')
        try:
            res = run_loadtest(args.repo, distributed=args.distributed,
                               fl_result_path=args.result_path,
                               fixture_plugin=args.fixture_plugin,
                               fixture_options=args.fixture_options,
                               options=options)
            logger.info('Report generated at %r' % res)
        except KeyboardInterrupt:
            sys.exit(1)
        finally:
            logger.info('Bye!')


if __name__ == '__main__':
    main()

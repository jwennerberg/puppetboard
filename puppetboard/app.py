from __future__ import unicode_literals
from __future__ import absolute_import

import glob
import os
import re
import logging
import collections
try:
    from urllib import unquote
except ImportError:
    from urllib.parse import unquote
from datetime import datetime, timedelta

from flask import (
    Flask, render_template, abort, url_for,
    Response, stream_with_context, redirect,
    request
    )
from flask_wtf.csrf import CsrfProtect
from functools import wraps

from pypuppetdb import connect

from puppetboard.forms import QueryForm
from puppetboard.utils import (
    get_or_abort, yield_or_stop,
    limit_reports, jsonprint
    )

csrf = CsrfProtect()
app = Flask(__name__)
csrf.init_app(app)

app.config.from_object('puppetboard.default_settings')
graph_facts = app.config['GRAPH_FACTS']
app.config.from_envvar('PUPPETBOARD_SETTINGS', silent=True)
graph_facts += app.config['GRAPH_FACTS']
app.secret_key = app.config['SECRET_KEY']
puppetfile_path = app.config['PUPPETFILE_PATH']
gerrit_host = app.config['GERRIT_HOST']
gerrit_project_name = app.config['GERRIT_PROJECT_NAME']

app.jinja_env.filters['jsonprint'] = jsonprint

puppetdb = connect(
    api_version=3,
    host=app.config['PUPPETDB_HOST'],
    port=app.config['PUPPETDB_PORT'],
    ssl_verify=app.config['PUPPETDB_SSL_VERIFY'],
    ssl_key=app.config['PUPPETDB_KEY'],
    ssl_cert=app.config['PUPPETDB_CERT'],
    timeout=app.config['PUPPETDB_TIMEOUT'],)

numeric_level = getattr(logging, app.config['LOGLEVEL'].upper(), None)
if not isinstance(numeric_level, int):
        raise ValueError('Invalid log level: %s' % loglevel)
logging.basicConfig(level=numeric_level)
log = logging.getLogger(__name__)


def stream_template(template_name, **context):
    app.update_template_context(context)
    t = app.jinja_env.get_template(template_name)
    rv = t.stream(context)
    rv.enable_buffering(5)
    return rv


@app.context_processor
def utility_processor():
    def now(format='%m/%d/%Y %H:%M:%S'):
        """returns the formated datetime"""
        return datetime.now().strftime(format)
    return dict(now=now)


@app.errorhandler(400)
def bad_request(e):
    return render_template('400.html'), 400


@app.errorhandler(403)
def bad_request(e):
    return render_template('403.html'), 400


@app.errorhandler(404)
def not_found(e):
    return render_template('404.html'), 404


@app.errorhandler(412)
def precond_failed(e):
    """We're slightly abusing 412 to handle missing features
    depending on the API version."""
    return render_template('412.html'), 412


@app.errorhandler(500)
def server_error(e):
    return render_template('500.html'), 500


def secret_key_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if app.secret_key is None:
            return render_template('secret_key_missing.html')
        return f(*args, **kwargs)
    return decorated_function


@app.route('/')
def index():
    """This view generates the index page and displays a set of metrics and
    latest reports on nodes fetched from PuppetDB.
    """
    # TODO: Would be great if we could parallelize this somehow, doing these
    # requests in sequence is rather pointless.
    prefix = 'com.puppetlabs.puppetdb.query.population'
    num_nodes = get_or_abort(
        puppetdb.metric,
        "{0}{1}".format(prefix, ':type=default,name=num-nodes'))
    num_resources = get_or_abort(
        puppetdb.metric,
        "{0}{1}".format(prefix, ':type=default,name=num-resources'))
    avg_resources_node = get_or_abort(
        puppetdb.metric,
        "{0}{1}".format(prefix, ':type=default,name=avg-resources-per-node'))
    metrics = {
        'num_nodes': num_nodes['Value'],
        'num_resources': num_resources['Value'],
        'avg_resources_node': "{0:10.0f}".format(avg_resources_node['Value']),
        }

    nodes = puppetdb.nodes(
        unreported=app.config['UNRESPONSIVE_HOURS'],
        with_status=True)

    nodes_overview = []
    stats = {
        'changed': 0,
        'unchanged': 0,
        'failed': 0,
        'unreported': 0,
        'noop': 0
        }

    for node in nodes:
        if node.status == 'unreported':
            stats['unreported'] += 1
        elif node.status == 'changed':
            stats['changed'] += 1
        elif node.status == 'failed':
            stats['failed'] += 1
        elif node.status == 'noop':
            stats['noop'] += 1
        else:
            stats['unchanged'] += 1

        if node.status != 'unchanged':
            nodes_overview.append(node)

    return render_template(
        'index.html',
        metrics=metrics,
        nodes=nodes_overview,
        stats=stats
        )


@app.route('/nodes')
def nodes():
    """Fetch all (active) nodes from PuppetDB and stream a table displaying
    those nodes.

    Downside of the streaming aproach is that since we've already sent our
    headers we can't abort the request if we detect an error. Because of this
    we'll end up with an empty table instead because of how yield_or_stop
    works. Once pagination is in place we can change this but we'll need to
    provide a search feature instead.
    """
    status_arg = request.args.get('status', '')
    nodelist = puppetdb.nodes(
        unreported=app.config['UNRESPONSIVE_HOURS'],
        with_status=True)
    nodes = []
    for node in yield_or_stop(nodelist):
        if status_arg:
            if node.status == status_arg:
                nodes.append(node)
        else:
            nodes.append(node)
    return Response(stream_with_context(
        stream_template('nodes.html', nodes=nodes)))


@app.route('/node/<node_name>')
def node(node_name):
    """Display a dashboard for a node showing as much data as we have on that
    node. This includes facts and reports but not Resources as that is too
    heavy to do within a single request.
    """
    def classes(name):
        for resource in puppetdb.catalog(name).resources:
            match = re.match( r'Class\[(.*)\]', resource, re.M|re.I)
            if match:
                c = match.group(1)
                if c != 'main':
                    yield c

    node = get_or_abort(puppetdb.node, node_name)
    facts = node.facts()
    classes = classes(node_name)
    reports = limit_reports(node.reports(), app.config['REPORTS_COUNT'])
    return render_template(
        'node.html',
        node=node,
        facts=yield_or_stop(facts),
        classes=yield_or_stop(classes),
        reports=yield_or_stop(reports),
        reports_count=app.config['REPORTS_COUNT'])


@app.route('/reports')
def reports():
    """Doesn't do much yet but is meant to show something like the reports of
    the last half our, something like that."""
    return render_template('reports.html')


@app.route('/reports/<node>')
def reports_node(node):
    """Fetches all reports for a node and processes them eventually rendering
    a table displaying those reports."""
    reports = limit_reports(yield_or_stop(
        puppetdb.reports('["=", "certname", "{0}"]'.format(node))), app.config['REPORTS_COUNT'])
    return render_template(
        'reports_node.html',
        reports=reports,
        nodename=node,
        reports_count=app.config['REPORTS_COUNT'])


@app.route('/report/latest/<node_name>')
def report_latest(node_name):
    """Redirect to the latest report of a given node. This is a workaround
    as long as PuppetDB can't filter reports for latest-report? field. This
    feature has been requested: https://tickets.puppetlabs.com/browse/PDB-203
    """
    node = get_or_abort(puppetdb.node, node_name)
    reports = get_or_abort(puppetdb._query, 'reports',
                           query='["=","certname","{0}"]'.format(node_name),
                           limit=1)
    if len(reports) > 0:
        report = reports[0]['hash']
        return redirect(url_for('report', node=node_name, report_id=report))
    else:
        abort(404)


@app.route('/report/<node>/<report_id>')
def report(node, report_id):
    """Displays a single report including all the events associated with that
    report and their status.

    The report_id may be the puppetdb's report hash or the
    configuration_version. This allows for better integration
    into puppet-hipchat.
    """
    reports = puppetdb.reports('["=", "certname", "{0}"]'.format(node))

    for report in reports:
        if report.hash_ == report_id or report.version == report_id:
            events = puppetdb.events('["=", "report", "{0}"]'.format(
                report.hash_))
            return render_template(
                'report.html',
                report=report,
                events=yield_or_stop(events))
    else:
        abort(404)


@app.route('/facts')
def facts():
    """Displays an alphabetical list of all facts currently known to
    PuppetDB."""
    facts_dict = collections.defaultdict(list)
    facts = get_or_abort(puppetdb.fact_names)
    for fact in facts:
        letter = fact[0].upper()
        letter_list = facts_dict[letter]
        letter_list.append(fact)
        facts_dict[letter] = letter_list

    sorted_facts_dict = sorted(facts_dict.items())
    return render_template('facts.html', facts_dict=sorted_facts_dict)


@app.route('/fact/<fact>')
def fact(fact):
    """Fetches the specific fact from PuppetDB and displays its value per
    node for which this fact is known."""
    # we can only consume the generator once, lists can be doubly consumed
    # om nom nom
    render_graph = False
    if fact in graph_facts:
        render_graph = True
    localfacts = [f for f in yield_or_stop(puppetdb.facts(name=fact))]
    return Response(stream_with_context(stream_template(
        'fact.html',
        name=fact,
        render_graph=render_graph,
        facts=localfacts)))


@app.route('/fact/<fact>/<value>')
def fact_value(fact, value):
    """On asking for fact/value get all nodes with that fact."""
    facts = get_or_abort(puppetdb.facts, fact, value)
    localfacts = [f for f in yield_or_stop(facts)]
    return render_template(
        'fact.html',
        name=fact,
        value=value,
        facts=localfacts)


@app.route('/query', methods=('GET', 'POST'))
@secret_key_required
def query():
    """Allows to execute raw, user created querries against PuppetDB. This is
    currently highly experimental and explodes in interesting ways since none
    of the possible exceptions are being handled just yet. This will return
    the JSON of the response or a message telling you what whent wrong /
    why nothing was returned."""
    if app.config['ENABLE_QUERY']:
        form = QueryForm()
        if form.validate_on_submit():
            if form.query.data[0] == '[':
                query = form.query.data
            else:
                query = '[{0}]'.format(form.query.data)
            result = get_or_abort(
                puppetdb._query,
                form.endpoints.data,
                query=query)
            return render_template('query.html', form=form, result=result)
        return render_template('query.html', form=form)
    else:
        log.warn('Access to query interface disabled by administrator..')
        abort(403)


@app.route('/metrics')
def metrics():
    metrics = get_or_abort(puppetdb._query, 'metrics', path='mbeans')
    for key, value in metrics.items():
        metrics[key] = value.split('/')[3]
    return render_template('metrics.html', metrics=sorted(metrics.items()))


@app.route('/metric/<metric>')
def metric(metric):
    name = unquote(metric)
    metric = puppetdb.metric(metric)
    return render_template(
        'metric.html',
        name=name,
        metric=sorted(metric.items()))


@app.route('/modules')
def modules():
    modules = []
    data = {}
    f = open(puppetfile_path, 'r')
    mod = False
    data = []
    info = {}
    for line in f.readlines():
        match = re.match( r'^mod [\'\"]?([a-z][a-z0-9\_]*)[\'\"]?\,$', line, re.I|re.M)
        if match:
            info = {}
            mod = True
            info['name'] = match.group(1)
        if mod:
            m_location = re.match( r'^\s*:git \=\> [\'\"]?([a-z0-9@\:\/\-\_\.]+)[\'\"]?,?$', line, re.I|re.M)
            if m_location:
                info['location'] = m_location.group(1)
                m_url = re.match( r'^ssh:\/\/[a-z0-9]*@?[a-z0-9\.\-\_]+:?[0-9]*\/(.*)$', info['location'], re.I|re.M)
                if m_url:
                    info['url'] = 'https://{0}/gitweb?p={1}.git;a=tree'.format(gerrit_host, m_url.group(1))
                else:
                    info['url'] = re.sub('git:', 'https:', info['location'])
            m_ref = re.match( r'^\s*:ref \=\> [\'\"]?([a-z0-9\-\.]+)[\'\"]?,?$', line, re.I|re.M)
            if m_ref:
                info['ref'] = m_ref.group(1)
        if line in ['\n', '\r\n']:
            if len(info) > 0:
                data.append(info)
                info = {}
    f.close()
    return render_template(
        'modules.html',
        modules=data)


@app.route('/yaml/<node_name>')
def yaml_node(node_name):
    if node_name is None:
        abort(404)
    url = 'https://{0}/gitweb?p={1}.git;a=blob_plain;f=fqdn/{2}.yaml;hb=refs/heads/master'.format(gerrit_host, gerrit_project_name, node_name)
    return render_template(
        'yaml_node.html',
        url=url)


@app.route('/repo')
def repo():
    url = 'https://{0}/gitweb?p={1}.git;a=tree'.format(gerrit_host, gerrit_project_name)
    return render_template(
        'repo.html',
        url=url)


@app.route('/inventory')
def inventory():
    inventory_dir = app.config['INVENTORY_DIR']
    reports = [ os.path.basename(x) for x in glob.glob('{0}/*.csv'.format(inventory_dir)) ]
    return render_template(
        'inventory.html',
        reports=reports)

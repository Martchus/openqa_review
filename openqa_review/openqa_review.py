#!/usr/bin/env python

"""
Review helper script for openQA.

# Inspiration

We want to gather information about the build status and condense this into a
review report.

E.g.

1. Go to the openQA Dashboard and select the latest build of the product you
want to review.

2. Walk through all red testcases for the product for all arches.

 - fix the needle
 - report a bug against openQA
 - report a bug against the product


Also review still failing test cases.

3. Add a comment to the overview page of the reviewed product using the
template generated by this script


# What it does

On calling the script it parses openQA status reports from openQA server
webpages and generates markdown text usable as template for review reports.

So far it is save to call it as it does not use or need any kind of
authentication and only reads the webpage. No harm should be done :-)

## feature list

 - Command line options with different modes, e.g. for markdown report generation
 - Strip optional "Build" from build number when searching for last reviewed
 - Support differing tests in test rows
 - Loading and saving of cache files (e.g. for testing)
 - Skip over '(reference ...)' searching for last reviewed
 - Yield last finished in case of no reviewed found in comments
 - Add proper handling for non-number build number, e.g. for 'SLE HA'
 - Tests to ensure 100% statement and branch coverage
 - Coverage analysis and test
 - Option to compare build against last reviewed one from comments section
 - Extended '--job-groups' to also accept regex terms
 - Add notice in report if architectures are not run at all
 - Support for explicit selection of builds not in job group display anymore
 - Add optional link to previous build for comparison for new issues
 - Option to specify builds for comparison
 - Support both python version 2 and 3
 - Human friendly progress notification and wait spinner
 - Accept multiple entries for '--job-group(-urls)'
 - Ensure report entries are in same alphabetical order with OrderedDict
 - tox.ini: Local tests, webtests, doctests, check with flake8
 - Generate version based on git describe
 - tests: Make slow webtests ignorable by marker
 - Add support to parse all job groups


# How to use

Just call it and see what happens or call this file with option '--help'.


# Design decisions

The script was designed to be a webscraping script for the following reasons

 * It should as closely resemble what the human review user encounters and not
   use any "hidden API" magic
 * "proof of concept": Show that it is actually possible using webscraping on
   a clean web design as openQA provides :-)
 * Do not rely on the annoyingly slow login and authentication redirect
   mechanism used on openqa.opensuse.org as well as openqa.opensuse.org


Alternatives could have been and still are for further extensions or reworks:

 * Use of https://github.com/os-autoinst/openQA-python-client and extend on
   that
 * Directly include all necessary meta-reports as part of openQA itself
 * Use REST or websockets API instead of webscraping


"""

# Python 2 and 3: easiest option
# see http://python-future.org/compatible_idioms.html
from __future__ import absolute_import
from future.standard_library import install_aliases  # isort:skip to keep 'install_aliases()'
install_aliases()
from future.utils import iteritems

import argparse
import datetime
import logging
import os.path
import re
import sys
from collections import defaultdict
from configparser import ConfigParser  # isort:skip can not make isort happy here
from string import Template
from urllib.parse import quote, unquote, urljoin

from bs4 import BeautifulSoup
from sortedcontainers import SortedDict

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from openqa_review.browser import Browser, DownloadError, add_load_save_args  # isort:skip


# treat humanfriendly as optional dependency
humanfriendly_available = False
try:
    from humanfriendly import AutomaticSpinner
    from humanfriendly.text import pluralize
    humanfriendly_available = True
except ImportError:  # pragma: no cover
    def pluralize(_1, _2, plural):
        return plural

logging.basicConfig()
log = logging.getLogger(sys.argv[0] if __name__ == "__main__" else __name__)
logging.captureWarnings(True)  # see https://urllib3.readthedocs.org/en/latest/security.html#disabling-warnings

config = None


CONFIG_PATH = os.path.expanduser('~') + '/.openqa_reviewrc'
CONFIG_USAGE = """
You are missing the mandatory configuration file with the proper format.
Example:
[product_issues]
system = bugzilla
# if username and password are not defined here, they will be stored in your
# local keyring if found
#username = user
#password = secret
base_url = https://%(username)s:%(password)s@apibugzilla.suse.com
"""


openqa_review_report_product_template = Template("""
**Date:** $now
**Build:** $build
$common_issues
<hr>
$arch_report
""")  # noqa: W291  # ignore trailing whitespace for forced line breaks

todo_review_template = Template("""
**TODO: review**
$new_issues$existing_issues""")

# TODO don't display sections if empty
openqa_review_report_arch_template = Template("""
**Arch:** $arch
**Status: $status_badge**
$new_product_issues$existing_product_issues$new_openqa_issues$existing_openqa_issues$todo_issues""")

status_badge_str = {
    'GREEN': '<font color="green">Green</font>',
    'AMBER': '<font color="#FFBF00">Amber</font>',
    'RED': '<font color="red">Red</font>',
}


class NotEnoughBuildsError(Exception):
    """Not enough finished builds found."""
    pass


def parse_summary(details):
    """parse and return build summary as dict."""
    return {i.previous.strip().rstrip(':').lower(): int(i.text) for i in details.find(id="summary").find_all(class_="badge")}

change_state = {
    ('result_passed', 'result_failed'): 'NEW_ISSUE',
    ('result_softfail', 'result_failed'): 'NEW_ISSUE',
    ('result_passed', 'result_softfail'): 'NEW_SOFT_ISSUE',
    ('result_failed', 'result_passed'): 'FIXED',  # fixed, maybe spurious, false positive
    ('result_softfail', 'result_passed'): 'FIXED',
    ('result_failed', 'result_failed'): 'STILL_FAILING',  # still failing or partial improve, partial degrade
    ('result_softfail', 'result_softfail'): 'STILL_SOFT_FAILING',
    ('result_failed', 'result_softfail'): 'IMPROVED',
    ('result_passed', 'result_passed'): 'STABLE',  # ignore or crosscheck if not fals positive
}

interesting_states_names = [i for i in set(change_state.values()) if i != 'STABLE'] + ['INCOMPLETE']


def status(entry):
    """return test status from entry, e.g. 'result_passed'."""
    # TODO to also get URLs to tests:
    #  test_urls = [i.find('a').get('href') for i in entry.find_all(class_='failedmodule')]
    # returns something like '/tests/167330/modules/welcome/steps/8'
    # to also return failed needles (if any):
    #  failed_needles = [BeautifulSoup(i.get('title'), 'html.parser').ul.li.text for i in entry.find_all(class_='failedmodule')]
    # returns something like 'inst-betawarning-20140602'
    return entry.i['class'][3]


def get_failed_needles(m):
    return [i.text for i in BeautifulSoup(m['title'], 'html.parser').find_all('li')] if m.get('title') else []


def get_test_details(entry):
    failedmodules = entry.find_all(class_='failedmodule')
    return {'href': entry.a['href'],
            'failedmodules': [{'href': m.a['href'], 'name': m.text.strip(), 'needles': get_failed_needles(m)} for m in failedmodules]
            }


def get_test_bugref(entry):
    bugref = entry.find(id=re.compile('^bug-'))
    if not bugref:
        return {}
    return {'bugref': re.sub('Bug\(s\) referenced: ', '', bugref.i['title']),
            'bugref_href': bugref.a['href']
            }


def get_state(cur, prev_dict):
    """Return change_state for 'previous' and 'current' test status html-td entries."""
    # TODO instead of just comparing the overall state we could check if
    # failing needles differ
    try:
        prev = prev_dict[cur['id']]
        state_dict = {'state': change_state.get((status(prev), status(cur)), 'INCOMPLETE')}
        # add more details, could be skipped if we don't have details
        state_dict.update({'prev': {'href': prev.find('a')['href']}})
    except KeyError:
        # if there is no previous we assume passed to mark new failing test as 'NEW_ISSUE'
        state_dict = {'state': change_state.get(('result_passed', status(cur)), 'INCOMPLETE')}
    state_dict.update(get_test_details(cur))
    state_dict.update(get_test_bugref(cur))
    return (cur['id'], state_dict)


def get_arch_state_results(arch, current_details, previous_details, output_state_results=False):
    test_results = current_details.find_all('td', id=re.compile(arch))
    test_results_previous = previous_details.find_all('td', id=re.compile(arch))
    # find differences from previous to current (result_X)
    test_results_dict = {i['id']: i for i in test_results}
    test_results_previous_dict = {i['id']: i for i in test_results_previous if i['id'] in test_results_dict.keys()}
    states = SortedDict(get_state(v, test_results_previous_dict) for k, v in iteritems(test_results_dict))
    # intermediate step:
    # - print report of differences
    interesting_states = SortedDict({k.split(arch + '_')[1]: v for k, v in iteritems(states) if v != 'STABLE'})
    if output_state_results:
        print("arch: %s" % arch)
        for state in interesting_states_names:
            print("\n%s:\n\t%s\n" % (state, ', '.join(k for k, v in iteritems(interesting_states) if v['state'] == state)))
    return interesting_states


def bugref_str(v, subject=None):
    return '[%s](%s%s)' % (v.get('bugref', 'NONE'), v.get('bugref_href', 'NONE'), " \"%s\"" % subject if subject else "")


def absolute_url(root, v):
    return urljoin(root, str(v['href']))


class Report(object):

    """Report generator with verbose_test parameter."""

    def __init__(self, root_url, verbose_test):
        """Construct report object with options."""
        self.root_url = root_url
        self.verbose_test = verbose_test

    def url(self, v):
        """Absolute url e.g. for test references."""
        return absolute_url(self.root_url, v)

    def new_issue(self, k, v):
        """Yield a report entry for one new issue based on verbosity."""
        report = {1: lambda k, v: '%s' % k,
                  2: lambda k, v: '***%s***: %s' % (k, self.url(v)),
                  3: lambda k, v: '***%s***: %s, failed modules:\n%s\n' % (k, self.url(v),
                                                                           '\n'.join(' * %s: %s' % (i['name'], self.url(i)) for i in v['failedmodules'])),
                  # separate 'reference URL' with space to prevent openQA comment parser to pickup ')' as part of URL
                  4: lambda k, v: '***%s***: %s (reference %s ), failed modules:\n%s\n' % (
                      k, self.url(v), self.url(v['prev']) if 'prev' in v.keys() else 'NONE',
                      '\n'.join(' * %s: %s %s' % (
                          i['name'], self.url(i), '(needles: %s)' % ', '.join(i['needles']) if i['needles'] else '')
                          for i in v['failedmodules'])),
                  }
        verbose_test = min(self.verbose_test, max(report.keys()))
        return report[verbose_test](k, v)


def query_issue(args, bugref, bugref_href):
    issue = {}
    if bugref.startswith('poo#'):
        b = Browser(args, '')
        issue_json = b.get_json(bugref_href + '.json')['issue']
        issue['status'] = issue_json['status']['name']
        issue['assignee'] = issue_json['assigned_to']['name'] if 'assigned_to' in issue_json else 'None'
        issue['subject'] = issue_json['subject']
    # bugref.startswith('bsc#') or bugref.startswith('boo#')
    else:
        b = Browser(args, config.get('product_issues', 'base_url') % {
            "username": config.get('product_issues', 'username'),
            "password": config.get('product_issues', 'password'),
        })
        bugid = int(bugref.replace('bsc#', '').replace('boo#', ''))
        issue_json = b.get_json('/jsonrpc.cgi?method=Bug.get&params=[{"ids":[%s]}]' % bugid)['result']['bugs'][0]
        issue['status'] = issue_json['status']
        if issue_json.get('resolution'):
            issue['status'] += " (%s)" % issue_json['resolution']
        issue['assignee'] = issue_json['assigned_to'] if 'assigned_to' in issue_json else 'None'
        issue['subject'] = issue_json['summary']
    return issue


def all_failures_one_bug(result_list, args, query_issue_status=False):
    line = '* %s -> ' % ', '.join([i['name'] for i in result_list])
    bug = result_list[0]
    if query_issue_status:
        try:
            issue = query_issue(args, bugref=bug['bugref'], bugref_href=bug['bugref_href'])
            line += bugref_str(result_list[-1], issue['subject']) + " (Ticket status: %(status)s, assignee: %(assignee)s)" % issue
        except DownloadError as e:  # pragma: no cover
            return line + bugref_str(result_list[-1]) + ' ' + str(e) + '\n'
    else:
        line += bugref_str(result_list[-1])
    return line + '\n'


def issue_listing(header, issues, show_empty=True):
    r"""
    Generate one issue listing section.

    @param header: Header string for section
    @param issues: Issues string, e.g. comma separated, may be an empty string
    @param show_empty: show empty sections if True and issues are an empty string

    >>> issue_listing('***new issues:***', 'None')
    '\n***new issues:***\n\nNone\n'
    >>> issue_listing('***Common issues:***', '')
    '\n***Common issues:***\n\n\n'
    >>> issue_listing('***no issues***', '', show_empty=False)
    ''
    """
    if not show_empty and issues == '':
        return ''
    return '\n' + header + '\n\n' + issues + '\n'


def common_issues(issues, show_empty=True):
    if not show_empty and issues == '':
        return ''
    return '\n' + '**Common issues:**' + '\n' + issues


def simple_joined_issues(results_by_bugref, state):
    if 'TODO' not in results_by_bugref:
        return ''
    issues = [i['name'] for i in results_by_bugref['TODO'] if i['state'] == state]
    if not issues:
        return ''
    return '* %s' % '\n* '.join(issues) + '\n'


def issue_type(bugref):
    return 'openqa' if re.match('poo#', bugref) else 'product'


def issue_state(result_list):
    # if any result was still failing the issue is regarded as existing
    return 'existing' if [i for i in result_list if i['state'] == 'STILL_FAILING'] else 'new'


def get_results_by_bugref(results, args):
    todo_ignore_tags = ['STILL_FAILING', 'NEW_ISSUE']
    if args.include_softfails:
        todo_ignore_tags += ['STILL_SOFT_FAILING', 'NEW_SOFT_ISSUE']
    results_by_bugref = defaultdict(list)
    for k, v in iteritems(results):
        if not re.match('(' + '|'.join(todo_ignore_tags) + ')', v['state']):
            continue
        new_key = v['bugref'] if ((args.bugrefs or args.query_issue_status) and 'bugref' in v) else 'TODO'
        v.update({'name': k})
        results_by_bugref[new_key].append(v)

    return results_by_bugref


def set_status_badge(states):
    # TODO pretty arbitrary
    if states.count('NEW_ISSUE') == 0 and states.count('STILL_FAILING') <= 1:
        status_badge = status_badge_str['GREEN']
    # still failing and soft issues allowed; TODO also arbitrary, just adjusted to test set
    elif states.count('NEW_ISSUE') == 0 and states.count('STILL_FAILING') <= 5:
        status_badge = status_badge_str['AMBER']
    else:
        status_badge = status_badge_str['RED']
    return status_badge


def generate_arch_report(arch, results, root_url, args):
    verbose_test = args.verbose_test
    show_empty = args.show_empty
    status_badge = set_status_badge([i['state'] for i in results.values()])

    results_by_bugref = SortedDict(get_results_by_bugref(results, args))
    issues = defaultdict(lambda: defaultdict(str))
    for bugref, result_list in iteritems(results_by_bugref):
        # if a ticket is known and the same refers to a STILL_FAILING scenario and any NEW_ISSUE we regard that as STILL_FAILING but just visible in more
        # scenarios, ...
        # ... else (no ticket linked) we don't group them as we don't know if it really is the same issue and handle them outside
        if not re.match('(poo|bsc|boo)#', bugref):
            continue
        # if any result was still failing the issue is regarded as existing
        query_issue_status = args.query_issue_status and re.match('(poo|bsc|boo)#', bugref)
        issues[issue_state(result_list)][issue_type(bugref)] += all_failures_one_bug(result_list, args, query_issue_status)

    # left do handle are the issues marked with 'TODO'
    if args.bugrefs or args.query_issue_status:
        issues['new']['todo'] = simple_joined_issues(results_by_bugref, 'NEW_ISSUE')
        issues['existing']['todo'] = simple_joined_issues(results_by_bugref, 'STILL_FAILING')
    else:
        report = Report(root_url, verbose_test)
        issues['new']['todo'] = '\n'.join('* %s' % report.new_issue(k, v) for k, v in iteritems(results) if v['state'] == 'NEW_ISSUE')
        issues['existing']['todo'] = '* ' + ', '.join(k for k, v in iteritems(results) if v['state'] == 'STILL_FAILING')

    if args.include_softfails:
        new_soft_fails_str = ', '.join(k for k, v in iteritems(results) if v['state'] == 'NEW_SOFT_ISSUE')
        if new_soft_fails_str:
            issues['new']['product'] += '\n* soft fails: ' + new_soft_fails_str + '\n'
        existing_soft_fails_str = ', '.join(k for k, v in iteritems(results) if v['state'] == 'STILL_SOFT_FAILING')
        if existing_soft_fails_str:
            issues['existing']['product'] += '\n* soft fails: ' + existing_soft_fails_str + '\n'

    todo_issues = todo_review_template.substitute({
        'new_issues': issue_listing('***new issues***', issues['new']['todo'], show_empty),
        'existing_issues': issue_listing('***existing issues***', issues['existing']['todo'], show_empty),
    })
    return openqa_review_report_arch_template.substitute({
        'arch': arch,
        'status_badge': status_badge,
        # everything that is 'NEW_ISSUE' should be product issue but if tests have changed content, then probably openqa issues
        # For now we can just not easily decide unless we use the 'bugrefs' mode
        'new_openqa_issues': issue_listing('**New openQA-issues:**', issues['new']['openqa'], show_empty),
        'existing_openqa_issues': issue_listing('**Existing openQA-issues:**', issues['existing']['openqa'], show_empty),
        'new_product_issues': issue_listing('**New Product bugs:**', issues['new']['product'], show_empty),
        'existing_product_issues': issue_listing('**Existing Product bugs:**', issues['existing']['product'], show_empty),
        'todo_issues': todo_issues if (issues['new']['todo'] or issues['existing']['todo']) else '',
    })


def generate_arch_reports(arch_state_results, root_url, args):
    return '<hr>'.join(generate_arch_report(k, v, root_url, args) for k, v in iteritems(arch_state_results))


def build_id(build_tag):
    return build_tag.text.lstrip('Build')


def find_builds(soup, running_threshold=0):
    """Find finished builds, ignore still running or empty."""
    def below_threshold(bar):
        threshold = float(running_threshold) if running_threshold is not None else 0
        return float(bar['style'].lstrip('width: ').rstrip('%')) <= threshold
    finished = [bar.parent.parent.parent for bar in soup.find_all(class_=re.compile("progress-bar-striped")) if below_threshold(bar)]

    def not_empty_build(bar):
        passed = re.compile("progress-bar-(success|passed|softfailed)")
        failed = re.compile("progress-bar-(danger|failed)")
        return not bar.find(class_=passed, style="width: 0%") or not bar.find(class_=failed, style="width: 0%")
    # filter out empty builds
    builds = [bar.find('a') for bar in finished if not_empty_build(bar)]
    log.debug("Found the following finished non-empty builds: %s" % ', '.join(build_id(b) for b in builds))
    return builds


def get_build_urls_to_compare(browser, job_group_url, builds='', against_reviewed=None, running_threshold=0):
    """
    From the job group page get URLs for the builds to compare.

    @param browser: A browser instance
    @param job_group_url: forwarded to browser instance
    @param builds: Builds for which URLs should be retrieved as comma-separated pair, w/o the word 'Build'
    @param against_reviewed: Alternative to 'builds', which build to retrieve for comparison with last reviewed, can be 'last' to automatically select the last
           finished
    @param running_threshold: Threshold of which percentage of jobs may still be running for the build to be considered 'finished' anyway
    """
    soup = browser.get_soup(job_group_url)
    finished_builds = find_builds(soup, running_threshold)
    build_url_pattern = re.compile('(?<=build=)([^&]*)')
    if builds:
        build_list = builds.split(',')
        # User has to be careful here. A page for non-existant builds is always
        # existant.
        for b in build_list:
            if len(b) < 4:
                log.warning("A build number of at least four digits is expected with leading zero, expect weird results.")  # pragma: no cover
    elif against_reviewed:
        # Could also find previous one with a comment on the build status,
        # i.e. a reviewed finished build
        # The build number itself might be prefixed with a redundant 'Build' which we ignore
        build_re = re.compile('[bB]uild: *(Build)?([\w@]*)(.*reference.*)?\n')
        # Assuming the most recent with a build number also has the most recent review
        try:
            last_reviewed = [build_re.search(i.text) for i in soup.find_all(class_='media-comment')][0].groups()[1]
        except (AttributeError, IndexError):
            log.info("No last reviewed build found for URL {}, reverting to two last finished".format(job_group_url))
            against_reviewed = None
        else:
            log.debug("Comparing specified build {} against last reviewed {}".format(against_reviewed, last_reviewed))
            build_to_review = build_id(finished_builds[0]) if against_reviewed == 'last' else against_reviewed
            assert len(build_to_review) <= len(last_reviewed) + 1, "build_to_review and last_reviewed differ too much to make sense"
            build_list = build_to_review, last_reviewed

    if builds or against_reviewed:
        assert len(finished_builds) > 0, "no finished builds found"
        current_url, previous_url = [build_url_pattern.sub(quote(i), finished_builds[0]['href']) for i in build_list]
    else:
        # find last finished and previous one
        if len(finished_builds) <= 1:
            raise NotEnoughBuildsError("not enough finished builds found")

        builds_to_compare = finished_builds[0:2]
        log.debug("Comparing build {} against {}".format(*[build_id(b) for b in builds_to_compare]))
        current_url, previous_url = [build.get('href') for build in builds_to_compare]
    log.debug("Found two build URLS, current: {} previous: {}".format(current_url, previous_url))
    return current_url, previous_url


def generate_product_report(browser, job_group_url, root_url, args=None):
    """Read overview page of one job group and generate a report for the product.

    @returns review report for product in Markdown format

    Example:
    >>> browser = Browser() # doctest: +SKIP
    >>> report = generate_product_report(browser, 'https://openqa.opensuse.org/group_overview/25', 'https://openqa.opensuse.org') # doctest: +SKIP
    """
    output_state_results = args.output_state_results if args.output_state_results else False
    try:
        current_url, previous_url = get_build_urls_to_compare(browser, job_group_url, args.builds, args.against_reviewed, args.running_threshold)
    except ValueError:
        raise NotEnoughBuildsError()

    # read last finished
    current_details = browser.get_soup(current_url)
    previous_details = browser.get_soup(previous_url)
    for details in current_details, previous_details:
        assert sum(int(badge.text) for badge in details.find_all(class_='badge')) > 0, \
            "invalid page with no test results found, make sure you specified valid builds (leading zero missing?)"
    current_summary = parse_summary(current_details)
    previous_summary = parse_summary(previous_details)

    changes = {k: v - previous_summary.get(k, 0) for k, v in iteritems(current_summary) if k != 'none' and k != 'incomplete'}
    log.info("Changes since last build:\n\t%s" % '\n\t'.join("%s: %s" % (k, v) for k, v in iteritems(changes)))

    def get_build_nr(url):
        return unquote(re.search('build=([^&]*)', url).groups()[0])
    build = get_build_nr(current_url)
    if args.verbose_test and args.verbose_test > 1:
        build += ' (reference %s)' % get_build_nr(previous_url)
    # for each architecture iterate over all
    cur_archs, prev_archs = (set(arch.text for arch in details.find_all('th', id=re.compile('flavor_'))) for details in [current_details, previous_details])
    archs = cur_archs
    if args.arch:
        assert args.arch in cur_archs, "Selected arch {} was not found in test results {}".format(args.arch, cur_archs)
        archs = [args.arch]
    missing_archs = prev_archs - cur_archs
    if missing_archs:
        log.info("%s missing completely from current run: %s" %
                 (pluralize(len(missing_archs), "architecture is", "architectures are"), ', '.join(missing_archs)))
    arch_state_results = SortedDict({arch: get_arch_state_results(arch, current_details, previous_details, output_state_results) for arch in archs})

    now_str = datetime.datetime.now().strftime('%Y-%m-%d - %H:%M')
    missing_archs_str = '\n * **Missing architectures**: %s' % ', '.join(missing_archs) if missing_archs else ''
    openqa_review_report_product = openqa_review_report_product_template.substitute({
        'now': now_str,
        'build': build,
        'common_issues': common_issues(missing_archs_str, args.show_empty),
        'arch_report': generate_arch_reports(arch_state_results, root_url, args),
    })
    return openqa_review_report_product


def parse_args():
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('-v', '--verbose',
                        help="Increase verbosity level, specify multiple times to increase verbosity",
                        action='count', default=1)
    parser.add_argument('-n', '--no-progress', action='store_true',
                        help="Be terse and only output the report, no progress indication")
    parser.add_argument('-s', '--output-state-results', action='store_true',
                        help='Additional plain text output of arch-specific state results, e.g. all NEW_ISSUE; on for "verbose" mode')
    parser.add_argument('--host', default='https://openqa.opensuse.org',
                        help='openQA host to access')
    parser.add_argument('--base-url', default='/',
                        help='openQA base url')
    parser.add_argument('-j', '--job-groups',
                        help="""Only handle selected job group(s), comma separated, e.g. \'openSUSE Tumbleweed Gnome\'.
                        A regex also works, e.g. \'openSUSE Tumbleweed\' or \'(Gnome|KDE)\'.""")
    parser.add_argument('-J', '--job-group-urls',
                        help="""Only handle selected job group(s) specified by URL, comma separated.
                        Skips parsing on main page and can actually save some seconds.""")
    builds = parser.add_mutually_exclusive_group()
    builds.add_argument('-b', '--builds',
                        help="""Select explicit builds, comma separated.
                        Specify as unambigous search terms, e.g. build number,
                        the full string, etc. Only works with single job-group/job-group-urls.
                        Default 'last' and 'previous'.""")
    builds.add_argument('-B', '--against-reviewed', metavar='BUILD',
                        help="""Compare specified build against last reviewed (as found in comments section).
                        E.g. if the last reviewed job was '0123' and you want to compare build '0128' against '0123',
                        specify just '0128' and the last reviewed job is found from the comments section if the comment
                        is sticking to the template format for review comments.
                        Special argument 'last' will compare the last finished build against the last reviewed one.""")
    test_details = parser.add_mutually_exclusive_group()
    test_details.add_argument('-T', '--verbose-test',
                              help='Increase test result verbosity level, specify multiple times to increase verbosity',
                              action='count', default=1)
    test_details.add_argument('-r', '--bugrefs', action='store_true',
                              help="""Parse \'bugrefs\' from test results comments and triage issues accordingly.
                              See https://progress.opensuse.org/projects/openqav3/wiki/Wiki#Show-bug-or-label-icon-on-overview-if-labeled-gh550
                              for details about bugrefs in openQA""")
    parser.add_argument('-R', '--query-issue-status', action='store_true',
                        help="""Query issue trackers for the issues found and report on their status and assignee. Implies "-r/--bugrefs" and
                        needs configuration file {} with credentials, see '--query-issue-status-help'.""".format(CONFIG_PATH))
    parser.add_argument('--query-issue-status-help', action='store_true',
                        help="""Shows help how to setup '--query-issue-status' configuration file.""")
    parser.add_argument('-a', '--arch',
                        help='Only single architecture, e.g. \'x86_64\', not all')
    parser.add_argument('--running-threshold', default=0,
                        help='Percentage of jobs that may still be running for the build to be considered \'finished\' anyway')
    parser.add_argument('--no-empty-sections', action='store_false', default=True, dest='show_empty',
                        help='Only show sections in report with content')
    parser.add_argument('--include-softfails', action='store_true', default=False,
                        help="""Also include softfails in reports.
                        Not included by default as less important and there is
                        no carry over for soft fails, i.e. there are no
                        bugrefs attached to these failures in most cases but
                        they should already carry bug references by other
                        means anyway.""")
    add_load_save_args(parser)
    args = parser.parse_args()
    if args.query_issue_status_help:
        print(CONFIG_USAGE)
        print("Expected file path: {}".format(CONFIG_PATH))
        sys.exit(0)
    return args


def get_job_groups(browser, root_url, args):
    if args.job_group_urls:
        job_group_urls = args.job_group_urls.split(',')
        log.info("Acting on specified job group URL(s): %s" % ', '.join(job_group_urls))
        job_groups = {i: url for i, url in enumerate(job_group_urls)}
    else:
        if args.no_progress or not humanfriendly_available:
            soup = browser.get_soup(root_url)
        else:
            with AutomaticSpinner(label='Retrieving job groups'):
                soup = browser.get_soup(root_url)
        job_groups = {i.text: absolute_url(root_url, i) for i in soup.select('h2 a[href^="/group_overview/"]')}
        log.debug("job groups found: %s" % job_groups.keys())
        if args.job_groups:
            job_pattern = re.compile('(%s)' % '|'.join(args.job_groups.split(',')))
            job_groups = {k: v for k, v in iteritems(job_groups) if job_pattern.search(k)}
            log.info("Job group URL for %s: %s" % (args.job_groups, job_groups))
    return SortedDict(job_groups)


def generate_report(args):
    verbose_to_log = {
        0: logging.CRITICAL,
        1: logging.ERROR,
        2: logging.WARN,
        3: logging.INFO,
        4: logging.DEBUG
    }
    logging_level = logging.DEBUG if args.verbose > 4 else verbose_to_log[args.verbose]
    log.setLevel(logging_level)
    log.debug("args: %s" % args)
    args.output_state_results = True if args.verbose > 1 else args.output_state_results

    root_url = urljoin(args.host, args.base_url)

    browser = Browser(args, root_url)
    job_groups = get_job_groups(browser, root_url, args)
    assert not (args.builds and len(job_groups) > 1), "builds option and multiple job groups not supported"
    assert len(job_groups) > 0, "No job groups were found, maybe misspecified '--job-groups'?"

    # for each job group on openqa.opensuse.org
    def one_report(job_group_url):
        try:
            log.info("Processing '%s'" % v)
            return generate_product_report(browser, job_group_url, root_url, args)
        except NotEnoughBuildsError:
            return "Not enough finished builds found"
    label = 'Gathering data and processing report'
    progress = 0
    report = ''

    def next_label(progress):
        return '%s %s %%' % (label, progress * 100 / len(job_groups.keys()))

    for k, v in iteritems(job_groups):
        if args.no_progress or not humanfriendly_available:
            report += '# %s\n\n%s' % (k, one_report(v)) + '\n---\n'
        else:
            with AutomaticSpinner(label=next_label(progress)):
                report += '# %s\n\n%s' % (k, one_report(v)) + '\n---\n'
        progress += 1
    if not args.no_progress:
        print("\n%s" % next_label(progress))  # It's nice to see 100%, too :-)
    return report


def load_config():
    global config
    config = ConfigParser()
    config_entries = config.read(CONFIG_PATH)
    if not config_entries:  # pragma: no cover
        print("Need configuration file '{}' for issue retrieval credentials".format(CONFIG_PATH))
        print(CONFIG_USAGE)
        sys.exit(1)


def main():  # pragma: no cover, only interactive
    args = parse_args()
    if args.query_issue_status:
        load_config()
    report = generate_report(args)
    print(report)


if __name__ == "__main__":
    main()

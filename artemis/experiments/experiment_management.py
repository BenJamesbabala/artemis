import getpass
from collections import OrderedDict
from importlib import import_module

from artemis.experiments.experiment_record import load_experiment_record, ExpInfoFields, \
    ExpStatusOptions, ARTEMIS_LOGGER, record_id_to_experiment_id
from artemis.experiments.experiments import load_experiment, GLOBAL_EXPERIMENT_LIBRARY
from artemis.fileman.config_files import get_home_dir
from artemis.general.hashing import compute_fixed_hash
from artemis.general.should_be_builtins import separate_common_items, izip_equal, detect_duplicates


def pull_experiments(user, ip, experiment_names, include_variants=True):
    """
    Pull experiments from another computer matching the given experiment name.

    :param user:
    :param ip:
    :param experiment_name:
    :param include_variants:
    :return:
    """
    import pexpect
    import sys
    # subprocess.call("rsync -a -m --include='**/*-demo_pdnn_revision*/*' --include='*/' --exclude='*' petered@146.50.28.7:~/.artemis/experiments/ ~/.artemis/experiments/", shell=True)


    if isinstance(experiment_names, basestring):
        experiment_names = [experiment_names]

    inclusions = ' '.join("--include='**/*-{exp_name}{variants}/*'".format(exp_name=exp_name, variants = '*' if include_variants else '') for exp_name in experiment_names)

    home = get_home_dir()

    command = "rsync -a -m -i {inclusions} --include='*/' --exclude='*' {user}@{ip}:~/.artemis/experiments/ {home}/.artemis/experiments/".format(
        inclusions=inclusions,
        user=user,
        ip=ip,
        home=home
        )
    password = getpass.getpass("Enter password for {}@{}:".format(user, ip))
    child = pexpect.spawn(command)
    code = child.expect([pexpect.TIMEOUT, 'password:'])
    if code == 0:
        print("Got unexpected output: %s %s" % (child.before, child.after))
        sys.exit()
    else:
        child.sendline(password)
    output = child.read()
    # try:
    # output = subprocess.call(command, shell=True)
    # except subprocess.CalledProcessError as e:
    #     raise Exception('rsync call threw an error: \n {}'.format(e.output))
    return output


def make_record_comparison_table(records, args_to_show=None, results_extractor = None, print_table = False):
    """
    Make a table comparing the arguments and results of different experiment records.  You can use the output
    of this function with the tabulate package to make a nice readable table.

    :param records: A list of records whose results to compare
    :param args_to_show: A list of arguments to show.  If none, it will just show all arguments
        that differ between experiments.
    :param results_extractor: A dict<str->callable> where the callables take the result of the
        experiment as an argument and return an entry in the table.
    :param print_table: Optionally, import tabulate and print the table here and now.
    :return: headers, rows
        headers is a list of of headers for the top of the table
        rows is a list of lists filling in the information.

    example usage:

        headers, rows = make_record_comparison_table(
            record_ids = [experiment_id_to_latest_record_id(eid) for eid in [
                'demo_fast_weight_mlp.multilayer_baseline.1epoch.version=mlp',
                'demo_fast_weight_mlp.multilayer_baseline.1epoch.full-gd.n_steps=1',
                'demo_fast_weight_mlp.multilayer_baseline.1epoch.full-gd.n_steps=20',
                ]],
            results_extractor={
                'Test': lambda result: result.get_best('test').score.get_score('test'),
                'Train': lambda result: result.get_best('test').score.get_score('train'),
                }
             )
        import tabulate
        print tabulate.tabulate(rows, headers=headers, tablefmt=tablefmt)
    """

    args = [rec.info.get_field(ExpInfoFields.ARGS) for rec in records]
    if args_to_show is None:
        common, separate = separate_common_items(args)
        args_to_show = [k for k, v in separate[0]]

    if results_extractor is None:
        results_extractor = {'Result': str}
    elif callable(results_extractor):
        results_extractor = {'Result': results_extractor}
    else:
        assert isinstance(results_extractor, dict)

    headers = args_to_show + results_extractor.keys()

    rows = []
    for record, record_args in izip_equal(records, args):
        arg_dict = dict(record_args)
        args_vals = [arg_dict[k] for k in args_to_show]
        results = record.get_result()
        rows.append(args_vals+[f(results) for f in results_extractor.values()])

    if print_table:
        import tabulate
        print tabulate.tabulate(rows, headers=headers, tablefmt='simple')
    return headers, rows


def load_lastest_experiment_results(experiments, error_if_no_result = True):
    """
    :param experiments:
    :param error_if_no_result:
    :return:
    """
    results = OrderedDict()
    for ex in experiments:
        record = ex.get_latest_record(err_if_none=error_if_no_result, only_completed=True)

        # record = load_latest_experiment_record(eid, filter_status=ExpStatusOptions.FINISHED)
        if record is None:
            if error_if_no_result:
                raise Exception("Experiment {} had no result.  Run this experiment to completion before trying to compare its results.".format(eid))
            else:
                ARTEMIS_LOGGER.warn('Experiment {} had no records.  Not including this in results'.format(eid))
        else:
            results[ex.get_id()] = record.get_result()
    if len(results)==0:
        ARTEMIS_LOGGER.warn('None of your experiments had any results.  Your comparison function will probably show no meaningful result.')
    return results


def select_experiments(user_range, exp_record_dict, return_dict=False):

    exp_filter = _filter_experiments(user_range, exp_record_dict)
    if return_dict:
        return OrderedDict((name, exp_record_dict[name]) for name in exp_record_dict if exp_filter[name])
    else:
        return [name for name in exp_record_dict if exp_filter[name]]


def _filter_experiments(user_range, exp_record_dict):

    if user_range in exp_record_dict:
        is_in = [k==user_range for k in exp_record_dict]
    else:
        number_range = interpret_numbers(user_range)
        if number_range is not None:
            # experiment_ids = [experiment_list[i] for i in number_range]
            is_in = [i in number_range for i in xrange(len(exp_record_dict))]
        elif user_range == 'all':
            # experiment_ids = experiment_list
            is_in = [True]*len(exp_record_dict)
        elif user_range.startswith('has:'):
            phrase = user_range[len('has:'):]
            # experiment_ids = [exp_id for exp_id in experiment_list if phrase in exp_id]
            is_in = [phrase in exp_id for exp_id in exp_record_dict]
        elif user_range.startswith('1diff:'):
            # select experiments whose arguments differ by one element from the selected experiments
            base_range = user_range[len('1diff:'):]
            base_range_exps = select_experiments(base_range, exp_record_dict) # list<experiment_id>
            all_exp_args_hashes = {eid: set(compute_fixed_hash(a) for a in load_experiment(eid).get_args().items()) for eid in exp_record_dict} # dict<experiment_id : set<arg_hashes>>
            # assert all_equal_length(all_exp_args_hashes.values()), 'All variants must have the same number of arguments' # Note: we diable this because we may have lists of experiments with different root functions.
            is_in = [any(len(all_exp_args_hashes[eid].difference(all_exp_args_hashes[other_eid]))<=1 for other_eid in base_range_exps) for eid in exp_record_dict]
        elif user_range.startswith('hasnot:'):
            phrase = user_range[len('hasnot:'):]
            # experiment_ids = [exp_id for exp_id in experiment_list if phrase not in exp_id]
            is_in = [phrase not in exp_id for exp_id in exp_record_dict]
        elif user_range in ('unfinished', 'invalid'):  # Return all experiments where all records are unfinished/invalid
            record_filters = _filter_records(user_range, exp_record_dict)
            # experiment_ids = [exp_id for exp_id in experiment_list if len(record_filters[exp_id])]
            is_in = [all(record_filters[exp_id]) for exp_id in exp_record_dict]
        else:
            raise Exception("Don't know how to use input '{}' to select experiments".format(user_range))

    return OrderedDict((exp_id, exp_is_in) for exp_id, exp_is_in in izip_equal(exp_record_dict, is_in))


def select_experiment_records(user_range, exp_record_dict, flat=True):
    """
    :param user_range:
    :param exp_record_dict: An OrderedDict<experiment_name: list<experiment_record_name>>
    :param flat: Return a list of experiment records, instead of an OrderedDict
    :return: if not flat, an An OrderedDict<experiment_name: list<experiment_record_name>>
        otherwise a list<experiment_record_name>
    """
    filters = _filter_records(user_range, exp_record_dict)
    filtered_dict = OrderedDict((k, [load_experiment_record(record_id) for record_id, f in izip_equal(exp_record_dict[k], filters[k]) if f]) for k in exp_record_dict.keys())
    if flat:
        return [record_id for records in filtered_dict.values() for record_id in records]
    else:
        return filtered_dict


def _filter_records(user_range, exp_record_dict):
    """
    :param user_range:
    :param exp_record_dict:
    :return: An OrderedDict<experiment_id -> list<True or False>> indicating whether each record from the given experiment passed the filter
    """

    def _bitwise(op, filter_set_1, filter_set_2):
        assert op in ('and', 'or')
        filter_set_3 = filter_set_1.copy()
        for k in filter_set_1.keys():
            filter_set_3[k] = [(a or b) if op=='or' else (a and b) for a, b in izip_equal(filter_set_1[k], filter_set_2[k])]
        return filter_set_3

    base = OrderedDict((k, [False]*len(v)) for k, v in exp_record_dict.iteritems())
    if user_range in exp_record_dict:  # User just lists an experiment
        base[user_range] = [True]*len(base[user_range])
        return base

    if '|' in user_range:
        return reduce(lambda a, b: _bitwise('or', a, b), [_filter_records(subrange, exp_record_dict) for subrange in user_range.split('|')])
    if '&' in user_range:
        return reduce(lambda a, b: _bitwise('and', a, b), [_filter_records(subrange, exp_record_dict) for subrange in user_range.split('&')])
    number_range = interpret_numbers(user_range)
    keys = exp_record_dict.keys()
    if number_range is not None:
        for i in number_range:
            base[keys[i]] = [True]*len(base[keys[i]])
    elif '.' in user_range:
        exp_rec_pairs = interpret_record_identifier(user_range)
        for exp_number, rec_number in exp_rec_pairs:
            base[keys[exp_number]][rec_number] = True
    elif user_range == 'old':
        for k, v in base.iteritems():
            base[k] = ([True]*(len(v)-1)+[False]) if len(v)>0 else []
    elif user_range == 'unfinished':
        for k, v in base.iteritems():
            base[k] = [load_experiment_record(rec_id).info.get_field(ExpInfoFields.STATUS) != ExpStatusOptions.FINISHED for rec_id in exp_record_dict[k]]
        # filtered_dict = OrderedDict((exp_id, [rec_id for rec_id in records if load_experiment_record(rec_id).info.get_field(ExpInfoFields.STATUS) != ExpStatusOptions.FINISHED]) for exp_id, records in exp_record_dict.iteritems())
    elif user_range == 'invalid':
        for k, v in base.iteritems():
            base[k] = [load_experiment_record(rec_id).is_valid() is False for rec_id in exp_record_dict[k]]
    elif user_range == 'all':
        for k, v in base.iteritems():
            base[k] = [True]*len(v)
    elif user_range == 'errors':
        for k, v in base.iteritems():
            base[k] = [load_experiment_record(rec_id).info.get_field(ExpInfoFields.STATUS)==ExpStatusOptions.ERROR for rec_id in exp_record_dict[k]]
    else:
        raise Exception("Don't know how to interpret subset '{}'".format(user_range))
    return base


def _filter_experiment_record_list(user_range, experiment_record_ids):
    if user_range=='all':
        return [True]*len(experiment_record_ids)
    elif user_range=='new':
        return detect_duplicates(experiment_record_ids, key=record_id_to_experiment_id, keep_last=True)
        # return [n for n, is_old in izip_equal(get_record_ids(), old) if not old]
    elif user_range=='old':
        return [not x for x in _filter_records(user_range, 'new')]
    elif user_range=='orphans':
        orphans = []
        for i, record_id in enumerate(experiment_record_ids):
            info = load_experiment_record(record_id).info
            if 'Module' in info:
                try:
                    import_module(info['Module'])
                    if not record_id_to_experiment_id(record_id) in GLOBAL_EXPERIMENT_LIBRARY:
                        orphans.append(True)
                    else:
                        orphans.append(False)
                except ImportError:
                    orphans.append(True)
            else:  # They must be old... lets kill them!
                orphans.append(True)
        return orphans
    else:
        which_ones = interpret_numbers(user_range)
        if which_ones is None:
            raise Exception('Could not interpret user range: "{}"'.format(user_range))
        filters = [False]*len(experiment_record_ids)
        for i in which_ones:
            filters[i] = True
        return filters


def select_experiment_records_from_list(user_range, experiment_records):
    return [rec_id for rec_id, f in izip_equal(experiment_records, _filter_experiment_record_list(user_range, experiment_records)) if f]


def interpret_record_identifier(user_range):
    """
    You can identify a single record with, eg 3.4, meaning "record 4 from experiment 3:.
    You can identify a range with, eg 3.1-3, meaning "records 3.1, 3.2, 3.3"
    :param user_range: The user input
    :return: A list of 2 tuples (exp_no, record_no).  e.g [(3, 4)], or [(3, 1), (3, 2), (3, 3)] in the above examples.
    """
    if ',' in user_range:
        parts = user_range.split(',')
        return [pair for p in parts for pair in interpret_record_identifier(p)]
    if '.' not in user_range:
        return None
    else:
        exp_number, record_numbers = user_range.split('.')
        return [(int(exp_number), rec_num) for rec_num in interpret_numbers(record_numbers)]


def interpret_numbers(user_range):
    """
    :param user_range: A string specifying a range of numbers.  Eg.
        interpret_numbers('4-6')==[4,5,6]
        interpret_numbers('4,6')==[4,6]
        interpret_numbers('4,6-9')==[4,6,7,8,9]
    :return: A list of integers, or None if the input is not numberic
    """
    if all(d in '0123456789-,' for d in user_range):
        numbers_and_ranges = user_range.split(',')
        numbers = [n for lst in [[int(s)] if '-' not in s else range(int(s[:s.index('-')]), int(s[s.index('-')+1:])+1) for s in numbers_and_ranges] for n in lst]
        return numbers
    else:
        return None
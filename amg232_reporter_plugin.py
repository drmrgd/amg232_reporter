#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Main Plugin code for the AMG-232 Reporter Plugin.
# 
# TODO:
#    List of things to still accomplish
#        1. Finish workingn on code.  Stopped at the html generation steps.
#           Want to generate a block html page that will have the following:
#               <barcode> <sample_name> <num_tp53_vars> <list_of_aa_muts?>
#           At the bottom will be a link to download a zip file of all of the
#           CSV files?
#        2. Error checking / handling.  how can we deal with sample failures
#        3. Optimization. Are there any steps that we can speed up?  Parallel
#           process?
#        4. Create an instance.html file to configure the plugin.  What if we
#           want to change some runtime opts, like reporting other genes as well?
#      
# version: 0.6.20180919
# 2018/09/17 - D Sims
################################################################################
"""
Main plugin script. Relies heavily on run_amg232_reporter_pipeline.py to 
run.
"""
import sys
import os
import inspect
import json
import subprocess
import argparse
import datetime
import shutil
import csv

from pprint import pprint as pp

# TODO: Not sure about these Django libs, but the bottom import template call
#       is not working for me.  Get plugin running, and then figure out how if
#       I need this and how to make it work.
#from django.conf import settings
#from django.template.loader import render_to_string
#from django.conf import global_settings
#global_settings.LOGGING_CONFIG=None

#from django import template

# Set up some logger defaults. 
loglevel = 'debug' # Min level to be reported to log.
logfile = sys.stderr

plugin_params = {}
plugin_result = {}
plugin_report = {}

barcode_summary = []
barcode_report = {}

def get_plugin_config():
    global plugin_params

    parser = argparse.ArgumentParser(description = __doc__)
    parser.add_argument('start_plugin_json', metavar='startplugin.json')
    parser.add_argument('barcodes_json', metavar='barcodes.json')
    parser.add_argument('-V', '--version', dest='version',
        help='Plugin version')
    parser.add_argument('--halt', dest='halt_on_failure', action='store_true',
        help='Stop plugin if any samples fail, otherwise, just skip the failed '
           'ones.')
    args = parser.parse_args()

    plugin_params['version'] = args.version

    # Get some filepaths and whatnot from start_plugin.json
    startplugin_data = json_read(args.start_plugin_json)
    wanted = ('analysis_dir', 'analysis_name', 'plugin_dir', 'plugin_name', 
        'results_dir', 'run_name')

    for elem in wanted:
        plugin_params[elem] = startplugin_data['runinfo'].get(elem, '')

    plugin_params['run_name'] = startplugin_data['expmeta'].get('run_name', '')
    plugin_params['analysis_name'] = startplugin_data['expmeta'].get(
        'results_name', plugin_params['plugin_name'])
    plugin_params['report_name'] = plugin_params['plugin_name'] + '.html'
    plugin_params['block_report'] = os.path.join(plugin_params['results_dir'],
        plugin_params['plugin_name'] + '_block.html')

    plugin_params['prefix'] = startplugin_data['expmeta'].get(
        'output_file_name_stem', '')
    if not plugin_params['prefix']:
        plugin_params['prefix'] = startplugin_data['expmeta'].get(
            'run_name', 'auto')
        if 'results_name' in startplugin_data['expmeta']:
            plugin_params['prefix'] += '_' + startplugin_data['expmeta']['results_name']
    resurl = startplugin_data['runinfo'].get('results_dir', '.')
    plgpos = resurl.find('plugin_out')
    if plgpos >= 0:
        plugin_params['results_url'] = os.path.join(
            startplugin_data['runinfo'].get('url_root', '.'), resurl[plgpos:] 
        )

    # Read barcodes.json and make a sample manifest.
    barcode_data = json_read(args.barcodes_json)

    samples = {}
    for bc in barcode_data:

        if barcode_data[bc]['nucleotide_type'] != 'RNA': 
            if barcode_data[bc]['sample'] != 'NTC':
                # If, for some reason, there is no sample name in the runinfo, then
                # we'll have problems.  Use the barcode as a fallback.
                samples[bc] = barcode_data[bc].get('sample') or bc

    plugin_params['samples'] = samples
    plugin_params['config'] = vars(args)
    writelog('d', pp(plugin_params, stream=sys.stderr))

def json_read(jdata):
    with open(jdata) as fh:
        return json.load(fh)

def writelog(level, msg):
    """
    Simple logger for this plugin. Can use Python's builtin, but just needed
    something more simple. Require a level in the form of 'i' for info, 'w'
    for 'warning', 'e' for 'error' and 'd' for 'debug'.
    """

    now = datetime.datetime.now().strftime('%c')
    log_levels = {
        'i' : (0, 'INFO:'),
        'w' : (1, 'WARN:'),
        'e' : (1, 'ERROR:'),
        'd' : (2, 'DEBUG:'),
    }
    if level is not None:
        tier, flag = log_levels[level[0].lower()]
        logstr = '{:26s} {:6s} {}\n'.format(now, flag, msg)

        if tier <= log_levels[loglevel[0].lower()][0]:
            logfile.write(logstr)
            logfile.flush()
    else:
        logfile.write('\t{:36s}\n'.format(msg))
        logfile.flush()

def __exit__(msg=None):
    """
    Simple custom exit method to help figure out where we terminated for 
    package dev.
    """
    sys.stderr.write('\n\033[38;5;196mExited at line: {}, with message: '
        '{}\033[00m\n'.format(inspect.stack()[1][2], msg))
    sys.exit()

def collect_vcfs(plugin_root):
    """
    Find the latest version of TVC run, and collect the VCFs for processing. 
    Assume that the run with the largest trailing number is the one that is 
    the most recent.
    """

    writelog('i', 'Checking for TVC data.')
    tvc_runs = [d for d in os.listdir(plugin_root) 
        if d.startswith('variantCaller')]

    if len(tvc_runs) < 1:
        # we should have a TVC run here based on the 'depends' class variable,
        # but check just in case.
        writelog('e', 'No TVC data available! Make sure to run TVC first and '
            'then this plugin!')
        sys.exit(1)
    writelog('d', 'TVC dirs: {}'.format(tvc_runs))
    largest = max([n.split('.')[1] for n in tvc_runs])
    latest_run = next(x for x in tvc_runs if x.endswith(largest))
    writelog('i', 'Found {} TVC runs. Choosing run {} for this analysis'.format(
        len(tvc_runs), latest_run))
    
    # Now that we have our desired TVC dir, use the 'results.json' file to
    # collect the VCF files, and cross-check with the barcodes data to ensure
    # that we didn't miss any.
    vcf_list = {}
    with open(os.path.join(plugin_root, latest_run, 'results.json')) as fh:
        tvc_results_json = json.load(fh)
        for f in tvc_results_json['files']:
            if f['type'] == 'variants_vcf_gz':
                if f['barcode'] in plugin_params['samples'].keys():
                    vcf_list.update({f['barcode'] : f['server_path']})

    tot_vcfs = len(vcf_list.keys())
    manifest = len(plugin_params['samples'])

    # Check the manifest against the file list.
    if tot_vcfs < 1:
        writelog('e', 'ERROR: There were no valid samples in the TVC run that '
            'we can process!')
        sys.exit(1)
    elif tot_vcfs != manifest:
        writelog('w', 'WARN: The number of found VCFs ({}) is not the same as '
            'the number indicated in the plan ({})! missing.'.format(
            tot_vcfs, manifest))
        missing = [x for x in plugin_params['samples'].keys() 
            if x not in vcf_list.keys()]
        writelog(None, 'Missing samples: {}'.format(missing))
        # Stop if we have requested halt_on_failure.
        if plugin_params['config']['halt_on_failure']:
            writelog(None, 'Exiting as requested.')
        else:
            writelog(None, 'Will continue with the samples we have.')
            for bc in missing:
                writelog('d', 'Removing barcode %s from manifest.' % bc)
                vcf_list.pop(bc, None)

    plugin_params['vcfs'] = {}

    # Copy the files to our plugin dir, and gunzip them.
    writelog('i', 'Gathering VCF files and gunzipping them.')
    for bc, vcf in vcf_list.items():
        new_path = os.path.join(plugin_params['results_dir'], 
            os.path.basename(vcf))
        writelog('d', 'new vcf path is: %s' % new_path)
        shutil.copy(vcf, new_path)
        subprocess.call(['gunzip', new_path])
        plugin_params['vcfs'].update({bc : new_path})

    writelog('i', 'Found {} VCF files. Done gathering VCF files for '
        'processing.'.format(len(plugin_params['vcfs'].keys())))
    writelog('d', 'All VCFs -> {}'.format(plugin_params['vcfs']))

def run_plugin():
    """
    Run the `run_amg232_pipeline.py` script on each VCF file, creating a dir of
    results for each sample.
    """

    # Create an initial empty barcodes summary report.
    writelog('i', 'Processing {} barcodes...'.format(
        len(plugin_params['vcfs'].keys())))
    updateBarcodeSummaryReport('', True)

    for barcode, vcf in sorted(plugin_params['vcfs'].items()):
        plugin_result[barcode] = {}
        sample_name = plugin_params['samples'][barcode]
        plugin_result[barcode]['sample_name'] = sample_name

        outdir = os.path.join(plugin_params['results_dir'], 
                sample_name + '_out')
        vcf = vcf.rstrip('.gz')
        new_path = os.path.join(outdir, os.path.basename(vcf))
        writelog('d', '\n  Pipeline Components:\n\tsample: {}\n\toutdir: {}\n\t'
            'old path: {}\n\tnew_path: {}\n'.format(sample_name, outdir, vcf,
            new_path))
    
        os.mkdir(os.path.join(plugin_params['results_dir'], outdir))
        shutil.move(vcf, new_path)

        writelog('i', 'Start processing sample %s...' % sample_name)
        cmd = [
            os.path.join(plugin_params['plugin_dir'], 
                'run_amg232_reporter_pipeline.py'),
            '-g', 'TP53',
            '-n', sample_name,
            '-o', outdir,
            new_path
        ]
        writelog('d', 'cmd -> {}'.format(cmd))
        p = subprocess.Popen(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE)
        stdout, stderr = p.communicate()
        writelog('d', stderr.decode('utf-8'))
        writelog('d', stdout.decode('utf-8'))
        if p.returncode != 0:
            writelog('e', 'Plugin failed during pipeline execution. Traced '
                'error is: ')
            writelog(None, stderr.decode('utf-8'))
            return 1

        results_file = os.path.join(
            outdir, 'TSVC_variants_%s_simple.amg-232_report.csv' % sample_name
        )
        plugin_result[barcode]['results_file'] = results_file
        result, num_vars, var_report = parse_results(results_file)
        plugin_result[barcode]['result'] = result
        plugin_result[barcode]['num_vars'] = num_vars
        plugin_result[barcode]['variant_report'] = var_report

        writelog('i', '{} result: {}'.format(sample_name, result))
        writelog('d', pp(var_report, stream=sys.stderr))
        writelog('i', 'Done with sample %s.' % sample_name)

def parse_results(results_csv):
    result = ''
    variants = []
    with open(results_csv) as fh:
        data = csv.DictReader(fh)
        variants = list(data)
        num_vars = len(variants)
        if num_vars == 0:
            result = 'No mutation detected.'
        else:
            result = 'Found %s TP53 variants.' % num_vars
    return result, num_vars, variants       

def updateBarcodeSummaryReport(barcode, autoRefresh=False):
    """
    Create a barcode summary (progress) report. Called before, during, and after
    barcodes are being analyzed.
    """
    global barcode_summary
    if barcode != '':
        result_data = plugin_result['barcodes'][barcode]
        report_data = plugin_report['barcodes'][barcode]
        errmsg = result_data.get('Error', '')
        sample = result_data['Sample Name']
        if sample == '':
            sample = 'None'
        if errmsg != '':
            details_link = "<span class='help' title='{}' style='color:red>{}</span>".format(errmsg, barcode)
            barcode_summary.append({
                'index' : len(barcode_summary),
                'barcode_name' : barcode,
                'barcode_details' : details_link,
                'sample' : sample,
                'sample_ID' : 'NA'
            })
        else:
            details_link = "<a target='_parent' href='{}' class='help'><span title='Click to view the detailed report for barcode {}'>{}</span><a>".format(
                os.path.join(barcode, plugin_params['report_name']),
                barcode,
                barcode
            )
            barcode_summary.append({
                'index' : len(barcode_summary),
                'barcode_name' : barcode,
                'barcode_details' : details_link,
                'sample' : sample,
            })
        render_context = {
            'autorefresh' : autoRefresh,
            'run_name' : plugin_params['prefix'], 
            'barcode_results' : json.dumps(barcode_summary)
        }
        if barcode_report:
            render_context.update(barcode_report)
            createReport(
                os.path.join(
                    plugin_params['results_dir'], 
                    plugin_params['report_name']
                ), 
                'barcode_summary.html', 
                render_context
            )

def plugin_main():
    # Get the plugin configuration and sample info
    get_plugin_config()

    # Write a nice start up message with some info about the config.
    writelog('i', 'AMG-232 Reporter has started')
    writelog('i', 'Run configuration:')

    writelog(None, 'Plugin name: {}'.format(plugin_params['plugin_name']))
    writelog(None, 'Plugin version: {}'.format(plugin_params['version']))
    writelog(None, 'Plugin root dir: {}'.format(plugin_params['plugin_dir']))
    writelog(None, 'Run name: {}'.format(plugin_params['run_name']))
    writelog(None, 'Analysis dir: {}'.format(plugin_params['analysis_dir']))
    writelog(None, 'Results dir: {}'.format(plugin_params['results_dir']))
    writelog(None, 'Barcodes:')
    for b, s in sorted(plugin_params['samples'].items()):
        writelog(None, '\t{}  {}'.format(b,s))
    writelog('i', 'There are {} samples to process.'.format(
        len(plugin_params['samples'].items())))

    # Figure out the latest TVC run, and get those VCFs for processing.
    plugin_out_root = os.path.dirname(plugin_params['results_dir'])

    # Collect the VCFs from TVC and stage them for processing.
    collect_vcfs(plugin_out_root)

    # Start running the pipeline on our samples.
    if run_plugin():
        return 1
    # TODO: How can I make the output html files.  Everything is now working OK, 
    #       but I need to figure out how to make the block.html and report.html 
    #       pages for the output.  I think I see how smaple ID is doing it (just
    #       look at the output from one of the runs), but the code to generate 
    #       these files is complicated.  Maybe I find a simpler way (as I did 
    #       with the actual plugin running to begin with!
    __exit__('Stopping after generating the pluign data. Need to finish figuring '
        'out how to generate output links.')

    writelog('i', 'AMG-232 Reporter has finished.\n')
    return 0

if __name__ == '__main__':
    exit(plugin_main())

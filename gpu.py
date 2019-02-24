#!/usr/bin/env python3
'''
curl -GET 'http://influxdb IP:端口/query?pretty=true' --data-urlencode "db=gpu_process" --data-urlencode "q=select * from node_gpu"

curl -POST http://influxdb IP:端口/query --data-urlencode "q=CREATE DATABASE gpu_process"
'''
import subprocess
import io
import csv
import collections
import json
import requests
import re
import os
# import threading as thd
import time
# influx db server url
posturl = 'http://10.0.4.235:9000/write?db=gpu_process'
node_name = os.getenv("NODE_NAME")
posturl   = os.getenv("INFLUXDB_URL")
print("node_name is :"+str(node_name))
print("influxdb_url is :"+ str(posturl))
if node_name == None:
    node_name = "no_name"
if posturl == None:
    posturl = 'http://10.0.4.235:9000/write?db=gpu_process'

def commandexists(shellcommand):
    status, output = subprocess.getstatusoutput(shellcommand)
    exists = status == 0
    if not exists:
        print("Could not execute: {0}".format(shellcommand))
    return exists


def command(args):
    return subprocess.check_output(args).decode()


def csvtodictdict(csvdata, colnames, keycols, fmtcols={}):
    '''
    Returns a dict of dicts from csv file with specified column names and primary key column
    accepts and optional element formatting per column as a dictionary of format functions
    '''
    fmtcols = collections.defaultdict(lambda: lambda x: x, **fmtcols)
    d = {}
    rows = csv.reader(csvdata)
    for row in rows:
        drow = {colname: fmtcols[colname](val) for colname, val in zip(colnames, row)}
        if isinstance(keycols, str):
            key = drow.pop(keycols)
        else:
            key = tuple([drow.pop(keycol) for keycol in keycols])
        d[key] = drow
    return d


def csvheaderargs(fmtcol, cols):
    return ",".join([fmtcol.format(col) for col in cols])


def commandtodictdict(baseargs, cols, keycols=None, queryargfmt="{0}", colargfmt="{0}", outputfmt={}, skipheader=False):
    queryarg = queryargfmt.format(csvheaderargs(colargfmt, cols))
    args = baseargs + [queryarg]
    # print(args)
    csvoutput = io.StringIO(command(args))
    if skipheader:
        csvoutput.readline()
    if keycols is None:
        keycols = cols[0]
    # print("sss:")
    # print(csvoutput)
    return csvtodictdict(csvoutput, cols, keycols, fmtcols=outputfmt)


def renamekeys(d, names):
    '''
    updates key names in d based on dict of old/new name pairs
    returning resulting updated dict
    '''
    for oldname, newname in names.items():
        d[newname] = d.pop(oldname)
    return d


def getContainer(containerid):
    container = command(['docker', 'inspect', containerid]).replace("\n", "").replace(" ", "");
    return json.loads(container)[0]

def main():
    # get results of all commands without container arguments
    dockerps = commandtodictdict(['docker', 'ps', '--format'],
                                 ['ID', 'Image', 'Ports'],
                                 keycols='ID',
                                 queryargfmt="'{0}'",
                                 colargfmt="{{{{.{0}}}}}",
                                 outputfmt={'ID': lambda s: s[1:]})
    dockerstats = commandtodictdict(['docker', 'stats', '--no-stream', '--format'],
                                    ['Container', 'MemUsage', 'CPUPerc'],
                                    keycols='Container',
                                    queryargfmt="'{0}'",
                                    colargfmt="{{{{.{0}}}}}",
                                    outputfmt={'Container': lambda s: s[1:]})
    unitstats = commandtodictdict(['nvidia-smi', '--format=csv'],
                                  ['gpu_uuid', 'utilization.gpu', 'utilization.memory','memory.total','memory.used','memory.free'],
                                  keycols='gpu_uuid',
                                  queryargfmt="--query-gpu={0}",
                                  outputfmt={'gpu_uuid': lambda s: s.lstrip()},
                                  skipheader=True)
    # print("un:")
    unitprocstats = commandtodictdict(['nvidia-smi', '--format=csv'],
                                      ['pid', 'process_name', 'gpu_uuid', 'used_memory'],
                                      keycols=['pid', 'gpu_uuid'],
                                      queryargfmt="--query-compute-apps={0}",
                                      outputfmt={'gpu_uuid': lambda s: s.lstrip()},
                                      skipheader=True)

    # map gpu_uuids to short ids in unit info rename columns
    shortunitids = {gpu_uuid: "{0}".format(shortid) for gpu_uuid, shortid in
                    zip(unitstats.keys(), range(len(unitstats)))}
    #print(shortunitids)
    # print("short")
    # print(shortunitids)



    colnames = {'utilization.gpu': 'used_gpu'}
    unitstats = {shortunitids[gpu_uuid]: renamekeys(stats, colnames) for gpu_uuid, stats in unitstats.items()}
    # print(unitstats)
    # node level monitor
    node_name = os.getenv("NODE_NAME")
    for k in unitstats.keys():
        mem_total = int(re.sub('\D',"", unitstats[k]['memory.total']))
        mem_used = int(re.sub('\D',"",unitstats[k]['memory.used']))
        mem_util = mem_used * 100.0 / mem_total
        data = '%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s' % (
            'node_gpu,host=', node_name, ',gpu_id=', k,
            ' gpu_util=', re.sub("\D","", unitstats[k]['used_gpu']), ',mem_util=',
            mem_util,',mem_used=',re.sub('\D',"",unitstats[k]['memory.used']), ',mem_total=',
            re.sub('\D',"", unitstats[k]['memory.total']),
            ",node=\"",node_name,
            "\",gid=",k,
            ",mem_free=", re.sub('\D',"", unitstats[k]['memory.free']))
        print(data)
        response = requests.post(posturl, data=data)
        print(response.status_code)
        # print(response.headers)

    # {'0': {'utilization.memory': ' 28 %', 'used_gpu': ' 49 %'}}

    unitprocstats = {(pid, shortunitids[gpu_uuid]): stats for (pid, gpu_uuid), stats in unitprocstats.items()}

    # reassign column names to valid python variable names for formatting

    # display fmt data
    basedisplaycols = collections.OrderedDict([('Container', 12),
                                               ('Image', 18)])
    optdisplaycols = collections.OrderedDict([('pid', 7),
                                              ('gpu_uuid', 8),
                                              ('used_memory', 12),
                                              ('used_gpu', 9)])
    displaycols = collections.OrderedDict(list(basedisplaycols.items()) +
                                          list(optdisplaycols.items()))

    # display fmt strings
    basedisplayfmt = '\t'.join(['{{{0}:{1}.{1}}}'.format(col, width) for col, width in basedisplaycols.items()])
    optdisplayfmt = '\t'.join(['{{{0}:{1}.{1}}}'.format(col, width) for col, width in optdisplaycols.items()])
    displayfmt = '\t'.join([basedisplayfmt, optdisplayfmt])

    # print rows of relevant container processes
    # (everything below a bit janky in terms of argument expectations and generalization)
    dockerall = {container: {**dockerps[container], **dockerstats[container]} for container in dockerstats.keys()}
    someunitsactive = False
    for container, dockerinfo in dockerall.items():
        # very particular incantation needed here for top options to function correctly:
        # https://www.projectatomic.io/blog/2016/01/understanding-docker-top-and-ps/
        pids = command(['docker', 'top', container, '-eo', 'pid']).split('\n')[1:-1]  # obviously could be a bit brittle
        containerunitstats = {(proc, unit): stat for (proc, unit), stat in unitprocstats.items() if proc in pids}
        tagflag = True
        host = node_name
        namespace = "-"
        podname = "-"
        user_name = "-"
        labels = getContainer(container)['Config']['Labels'];
        mlgpu = "False"
        if('mlgpu' in labels.keys()):
            mlgpu = labels['mlgpu']
        if mlgpu == 'mlgpu':
            someunitsactive = True
            for (pid, gpu_uuid), stats in containerunitstats.items():
                tagflag = False
                labels = getContainer(container)['Config']['Labels'];
                if('io.kubernetes.pod.name' in labels.keys()):
                    podname = labels['io.kubernetes.pod.name']
                    namespace = labels['io.kubernetes.pod.namespace']
                    if "turing-gpu-notebook-" in podname:
                        user_name = podname.split("turing-gpu-notebook-")[1].rsplit("-",2)[0]
                    else:
                        user_name = podname.split("-")[1]
                gpu_id = gpu_uuid
                containerid = container
                pid = pid
                procressname = stats['process_name'].replace(" ","")
                used_memory = re.sub("\D","",stats['used_memory'])
                gpu_util = re.sub("\D", "", unitstats[gpu_uuid].get('used_gpu'))
                data = '%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s' % (
                    'pod_gpu,host=', host, ',namespace=', namespace, ',gpu_id=', gpu_id, ',containerid=',
                    containerid, ',pid_t=', pid, ',gpu_util_t=', gpu_util, ',user_name_t=', user_name,
                    " procressname=\"", procressname,
                    "\",podname=\"", podname,
                    "\",pid=\"", pid,
                    "\",gpu_id_v=\"", gpu_id,
                    "\",node=\"", node_name,
                    "\",user_name=\"", user_name,
                    "\",gpu_util=", gpu_util,
                    ",used_memory=", used_memory)
                print(data)
                response = requests.post(posturl, data=data)
                print(response.status_code)
            if tagflag:
                p=os.popen("docker exec -i "+container+" nvidia-smi -L |cut -d '(' -f2 |cut -d ')' -f1|cut -d ' ' -f2")
                no_used_gpu_uuid=p.read()
                p.close()
                no_used_gpu_uuid_list = str(no_used_gpu_uuid).split()
                for gpu_i in range (0,len(no_used_gpu_uuid_list)):
                    gpu_id = shortunitids.get(no_used_gpu_uuid_list[gpu_i])
                    if ('io.kubernetes.pod.name' in labels.keys()):
                        podname = labels['io.kubernetes.pod.name']
                        namespace = labels['io.kubernetes.pod.namespace']
                        if "turing-gpu-notebook-" in podname:
                            user_name = podname.split("turing-gpu-notebook-")[1].rsplit("-", 2)[0]
                        else:
                            user_name = podname.split("-")[1]
                    containerid = container
                    pid = "-"
                    procressname = "-"
                    node_name = host
                    gpu_util = 0
                    used_memory = 0
                    data = '%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s' % (
                        'pod_gpu,host=', host, ',namespace=', namespace, ',gpu_id=', gpu_id, ',containerid=',
                        containerid, ',pid_t=', pid, ',gpu_util_t=', gpu_util,',user_name_t=',user_name,
                    " procressname=\"", procressname,
                    "\",podname=\"", podname,
                    "\",pid=\"", pid,
                    "\",gpu_id_v=\"",gpu_id,
                    "\",node=\"", node_name,
                    "\",user_name=\"", user_name,
                    "\",gpu_util=", gpu_util,
                    ",used_memory=", used_memory)
                    print(data)
                    response = requests.post(posturl, data=data)
                    print(response.status_code)

    if not someunitsactive:
        print("\n\t\t no gpu units being used by docker containers ")

if __name__ == '__main__':
    # check for existence of docker and nvidia-smi commands
    if commandexists('docker') and commandexists('nvidia-smi'):
        while 1:
            main()
            time.sleep(10)
        # main()
        # thd.Timer(10, main()).start()
    else:
        print('Command(s) not found')
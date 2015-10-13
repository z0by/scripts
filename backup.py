import os
import sys
sys.path.append(os.path.abspath('lib'))
sys.path.append("/usr/lib/python")

import socket
import subprocess
import httplib2
import ConfigParser

from period import in_period
import time

# prints non-fatal error
def nonFatalError(e):
    blMsg('warn', "Non-Fatal Error: " + str(e))

# backup event class, that will be push to the dashboard-server
class BackupEvent:
    eventType = ""          # onAppStart, onAppWait, onBackupStart, onBackupEnd, onAppEnd, onGetStatus
    fields = {}             # message fields

    # onAppStart: serverName, serverLA
    # onAppWait: serverName, serverLA, serviceName
    # onAppEnd: serverName, serverLA, exitStatus, exitMessage
    # onBackupStart: serverName, serverLA, serviceName, backupServer, resourceName
    # onBackupEnd: serverName, serverLA, serviceName, backupServer, resourceName, backupSuccess, backupErrorString
    # onGetStatus: serverName, serverLA, serviceName, backupStatus, backupError, backupCount, backupEarliestDate, backupNewestDate

    cfg = ""

    # pushing the event to dashboard server
    
    def push(self):
        h = httplib2.Http()
        self.fields['serverName'] = cfg['source']['name']
        self.fields['serverLA'] = os.getloadavg()
        self.fields['type'] = self.eventType
        self.fields['key'] = cfg['push_key']

        try:
            resp, content = h.request(self.cfg['status_url'], "POST", json.dumps(self.fields))

        except Exception as e:
            blMsg('warn', 'Pushing event failed: ' + str(e))
            return

def __afterSuccessBackup(sTo, evt, sTargetInfo, sObjectName):
    # delete old backups automatically
    
    global cfg

    if 'user' in cfg['target']:
        stgUser = cfg['target']['user']
    else:
        stgUser = 'backups'
	
    cmd_rdiffbackup = ['/usr/bin/rdiff-backup',
        '--list-increments',
        '--parsable-output',
        stgUser+'@'+cfg['target']['server']+'::'+sTo
    ]

    p = subprocess.Popen(' '.join(cmd_rdiffbackup), shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    ce, cd = p.communicate()
    rt = p.returncode

    if rt == 0:
        rd_info = ce.split("\n")

        if (len(rd_info) > 2) or (len(rd_info) == 1):
            rd_first = rd_info[0]
            rd_last = rd_info[-2]
        
        else:
            rd_first = "0"
            rd_last = "0"

        rd_first = rd_first.split(" ")[0]
        rd_last = rd_last.split(" ")[0]

        evt.eventType = "onGetStatus"
        evt.fields = {
            'serviceName': sTargetInfo['name'],
            'resourceName': sObjectName,
            'backupStatus': True,
            'backupCount': len(rd_info),
            'backupEarliestDate': rd_first,
            'backupNewestDate': rd_last
        }

        evt.push()

    else:
        evt.eventType = "onGetStatus"
        evt.fields = {
            'serviceName': sTargetInfo['name'],
            'backupStatus': False,
            'backupError': ce + cd
        }

        evt.push()
        blMsg('warn', "Target " + sTargetInfo['name'] + ", backup post-check failed: " + ce + cd)

        return False
        # backup status failed
        # we can post event about failed backup

    cmd_rdiffbackup = ['/usr/bin/rdiff-backup',
	    '--remove-older-than ' + str(cfg['target']['max_age_days']),
	    '--force',
	    stgUser+'@'+cfg['target']['server']+'::'+sTo
    ]
    
    p = subprocess.Popen(' '.join(cmd_rdiffbackup), shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    ce, cd = p.communicate()
    rt = p.returncode

    if rt == 0:
        return True
    else:
        evt.eventType = 'fatalError'
        evt.eventMessage = 'Post backup process failed:' + ce
        evt.push()
        blMsg('warn', evt.eventMessage)

        return False

# backup command builder (to rdiff-backup)
    
def __buildBackupCommand(sFrom, sTo, sTargetInfo):
    global cfg

    addConds = []

    if 'user' in cfg['target']:
        stgUser = cfg['target']['user']
    else:
        stgUser = 'backups'

    # exclude by regexp
    if 'exclude' in sTargetInfo:
        for val in sTargetInfo['exclude']:
            addConds.append("--exclude '" + val + "'")

    if 'exclude-regexp' in sTargetInfo:
        addConds.append("--exclude-regexp '" + sTargetInfo['exclude-regexp'] + "'")

    # exclude by shell pattern
    if 'exclude-shell' in sTargetInfo:
        addConds.append("--exclude '" + sTargetInfo['exclude-shell'] + "'")

    # include by regexp
    if 'include-regexp' in sTargetInfo:
        addConds.append("--include-regexp '" + sTargetInfo['include-regexp'] + "'")

    # include by shell pattern
    if 'include-shell' in sTargetInfo:
        addConds.append("--include '" + sTargetInfo['include-shell'] + "'")

    cmd_rdiffbackup = ['/usr/bin/rdiff-backup', 
        '--create-full-path',
        '--force',
        '--ssh-no-compression',
	    '--preserve-numerical-ids',
	    '--no-file-statistics',
        ' '.join(addConds),
	    sFrom,
	    stgUser+'@'+cfg['target']['server']+'::'+sTo
    ]

    return cmd_rdiffbackup

# main function
def __coreExec():
    # backup processing core
    
    global isCore
    global cfg

    # program execution warnings
    warnings = []

    # correct core initation marker
    isCore = 1
    
    # general variables
    srvName = cfg['source']['name']
    tgtPath = cfg['target']['path']
    evt = BackupEvent()
    evt.cfg = cfg
    
    # checking syntax
    if srvName == '':
        raise Exception("Empty server name!")

    if tgtPath == '':
        raise Exception("Invalid target path!")

    # transforming server name macros
    srvName = srvName.replace("%h", socket.gethostname()).lower()
    cfg['source']['name'] = srvName

    evt.eventType = "onAppStart"
    evt.push()

    blMsg('info', 'Requested Server run period: ' + cfg['source']['runat'])

    # step-by-step in targets    
    for srvTarget in cfg['source']['targets']:
        blMsg('info', 'Starting to run target ' + srvTarget['name'] + ', type ' + srvTarget['type'] + ', target run period: ' + srvTarget['runat'])

        if not in_period(srvTarget['runat']):
            blMsg('info', 'Target ' + srvTarget['name'] + ' skipped, because not in period!')
            continue

        # transforming taregt paths
        tgtPath2 = tgtPath.replace(
		    '%t', srvTarget['name']).replace(
		    "%n", srvName).lower()

        # processing mysql backup
        if srvTarget['type'] == 'mysql':
            # mysql common variables definitions

            serviceAuthType = "config"
            mysql_args = []

            try:
                if 'auth_type' in srvTarget:
                    if srvTarget['auth_type'] == 'my.cnf':
                        serviceAuthType = "my.cnf"
                        myCnfFile = srvTarget['my.cnf']

                if serviceAuthType == "config":
                    mysql_args = ["--user=" + srvTarget['user'], "--password=" + srvTarget['password']]
                elif serviceAuthType == "my.cnf":
                    mycnfCfg = ConfigParser.ConfigParser()
                    mycnfCfg.read(myCnfFile)

                    if mycnfCfg.has_section("client"):
                        if mycnfCfg.has_option("client", "password"):
                            mysqlPass = mycnfCfg.get("client", "password")

                        if mycnfCfg.has_option("client", "pass"):
                            mysqlPass = mycnfCfg.get("client", "pass")

                        if mycnfCfg.has_option("client", "user"):
                            mysqlUser = mycnfCfg.get("client", "user")
                        else:
                            mysqlUser = "root"

                    mysql_args = ["--user=" + mysqlUser, "--password=" + mysqlPass]

            except Exception as e:
                blMsg('warn', 'Target ' + srvTarget['name'] + ' - cannot get authentication data for MySQL: ' + str(e))
                warnings.append('Target ' + srvTarget['name'] + ' - cannot get authentication data for MySQL: ' + str(e))
                continue

            # if port set
            if 'port' in srvTarget:
                mysql_args.append("--port=" + srvTarget['port'])
                mysql_args.append("--protocol=tcp")
		
            # if host set
            if 'host' in srvTarget:
                mysql_args.append("--host=" + srvTarget['host'])
	
            # temporary directory to dump in
            if 'tmpdir' in srvTarget:
                tmpdir = srvTarget['tmpdir']
            else:
                tmpdir = "/home/db"
	    
            if not os.path.exists(tmpdir):
                os.makedirs(tmpdir)

            # command struct for mysql
            cmd_listdb = mysql_args[:]
            cmd_listdb.insert(0, "/usr/bin/mysql")
            cmd_listdb.append("--execute='SHOW DATABASES;'")
            cmd_listdb.append("| grep -v mysql50")

            # command struct for mysqldump
            cmd_backup = mysql_args[:]
            cmd_backup.insert(0, 'mysqldump')	# command
            cmd_backup.append("-qQceRf")	# quick and extended inserts, routines, events and force
            cmd_backup.append("--skip-comments") # no comments
            cmd_backup.append("--lock-tables=false") # no lock tables

            evt.eventType = "onBackupStart"
            evt.fields = {
                'serviceName': srvTarget['name'],
                'backupServer': cfg['target']['server'],
                'resourceName': 'MySQL Databases listing',
            }

            evt.push()
            
            # getting db list
            p = subprocess.Popen(' '.join(cmd_listdb), shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            ce, cd = p.communicate()
            rt = p.returncode

            if rt != 0:
                errstr = 'MySQL Backup Failed: Error getting db list: ' + cd + '. The command was: ' + ' '.join(cmd_listdb)

                evt.eventType = "onBackupEnd"
                evt.fields = {
                    'serviceName': srvTarget['name'],
                    'backupServer': cfg['target']['server'],
                    'resourceName': 'MySQL Databases listing',
                    'backupSuccess': False,
                    'backupErrorString': errstr
                }

                evt.push()

                blMsg('warn', errstr )
                warnings.append(errstr)
                continue;

            else:
                evt.eventType = "onBackupEnd"
                evt.fields = {
                    'serviceName': srvTarget['name'],
                    'backupServer': cfg['target']['server'],
                    'resourceName': 'MySQL Databases listing',
                    'backupSuccess': True
                }

                evt.push()

            for dbName in ce.split():
                while (not in_period(cfg['source']['runat'])):
                    blMsg('info', 'Waiting for the acceptable period...')
                    time.sleep(60)
		    
                # skipping incorrect DBs (likes lost+found)
                if dbName == 'Database':
                    continue
		
                if dbName == 'information_schema':
                    continue

                # appending dbname
                cmd_backup_rt = cmd_backup[:]
                cmd_backup_rt.append(dbName)

                blMsg('info', 'Dumping database ' + dbName)

                # configuring event
                evt.eventType = "onBackupStart"
                evt.fields = {
                    'serviceName': srvTarget['name'],
                    'backupServer': cfg['target']['server'],
                    'resourceName': dbName
                }

                evt.push()

                # launching backup process to tmpdir!
                p = subprocess.Popen(' '.join(cmd_backup_rt) + ' > ' + tmpdir + "/dump.sql", shell=True, stderr=subprocess.PIPE)
                ce, cd = p.communicate()
                rt = p.returncode

                if rt != 0:
                    errstr = 'mysqldump failed, db: ' + dbName + ', error: ' + cd

                    evt.eventType = "onBackupEnd"
                    evt.fields = {
                        'serviceName': srvTarget['name'],
                        'backupServer': cfg['target']['server'],
                        'resourceName': dbName,
                        'backupSuccess': False,
                        'backupErrorString': errstr
                    }

                    evt.push()

                    blMsg('warn', errstr)
                    warnings.append(errstr)
                    continue

                else:
                    # launching backup process to remote!
                    p = subprocess.Popen(' '.join(__buildBackupCommand(tmpdir, tgtPath2 + '/' + dbName, srvTarget)), shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    ce, cd = p.communicate()
                    rt = p.returncode

                    if rt != 0:
                        errstr = 'rdiff-backup failed, error: ' + ce + cd

                        evt.eventType = "onBackupEnd"
                        evt.fields = {
                            'serviceName': srvTarget['name'],
                            'backupServer': cfg['target']['server'],
                            'resourceName': dbName,
                            'backupSuccess': False,
                            'backupErrorString': errstr
                        }

                        evt.push()

                        blMsg('warn', errstr)
                        warnings.append(errstr)

                        continue
			
                    else:
                        evt.eventType = "onBackupEnd"
                        evt.fields = {
                            'serviceName': srvTarget['name'],
                            'backupServer': cfg['target']['server'],
                            'resourceName': dbName,
                            'backupSuccess': True,
                        }

                        evt.push()
                        __afterSuccessBackup(tgtPath2 + '/' + dbName, evt, srvTarget, dbName)
                        blMsg('info', "Backup uploaded, " + dbName)
                        continue

        # files target
        # expected vars: synctype, root
        if srvTarget['type'] == 'files':
            syncRoot = srvTarget['root']

            while (not in_period(cfg['source']['runat'])):
                blMsg('info', 'Waiting for the acceptable period...')
                time.sleep(60)
	    
            if 'synctype' in srvTarget:
                if srvTarget['synctype'] == 'by-dir':
                    for dirName in os.listdir(syncRoot):
                        while (not in_period(cfg['source']['runat'])):
                            blMsg('info', 'Waiting for the acceptable period...')
                            time.sleep(60)
			
                        if not os.path.isdir(os.path.join(syncRoot, dirName)):
                            continue
			    
                        # launching backup process to remote!
                        blMsg("info", ' '.join(__buildBackupCommand(os.path.join(syncRoot, dirName), tgtPath2 + '/' + dirName, srvTarget)))

                        evt.eventType = "onBackupStart"
                        evt.fields = {
                            'serviceName': srvTarget['name'],
                            'backupServer': cfg['target']['server'],
                            'resourceName': dirName
                        }

                        p = subprocess.Popen(' '.join(__buildBackupCommand(os.path.join(syncRoot, dirName), tgtPath2 + '/' + dirName, srvTarget)), shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                        ce, cd = p.communicate()
                        rt = p.returncode

                        if rt != 0:
                            errstr = 'rdiff-backup failed, error: ' + ce + cd

                            evt.eventType = "onBackupEnd"
                            evt.fields = {
                                'serviceName': srvTarget['name'],
                                'backupServer': cfg['target']['server'],
                                'resourceName': dirName,
                                'backupSuccess': False,
                                'backupErrorString': errstr
                            }

                            evt.push()

                            blMsg('warn', errstr)
                            warnings.append(errstr)
                            continue

                        else:
                            evt.eventType = "onBackupEnd"
                            evt.fields = {
                                'serviceName': srvTarget['name'],
                                'backupServer': cfg['target']['server'],
                                'resourceName': dirName,
                                'backupSuccess': True,
                            }

                            evt.push()

                            __afterSuccessBackup(tgtPath2 + '/' + dirName, evt, srvTarget, dirName)
                            blMsg('info', "Backup uploaded, " + dirName)
                            continue
			
            else:
                # launching backup process to remote!
                blMsg("info", ' '.join(__buildBackupCommand(syncRoot, tgtPath2, srvTarget)))

                evt.eventType = "onBackupStart"
                evt.fields = {
                    'serviceName': srvTarget['name'],
                    'backupServer': cfg['target']['server'],
                    'resourceName': srvTarget['root']
                }

                p = subprocess.Popen(' '.join(__buildBackupCommand(syncRoot, tgtPath2, srvTarget)), shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                ce, cd = p.communicate()
                rt = p.returncode

                if rt != 0:
                    errstr = 'rdiff-backup failed, error: ' + ce + cd

                    evt.eventType = "onBackupEnd"
                    evt.fields = {
                        'serviceName': srvTarget['name'],
                        'backupServer': cfg['target']['server'],
                        'resourceName': srvTarget['root'],
                        'backupSuccess': False,
                        'backupErrorString': errstr
                    }

                    evt.push()

                    blMsg('warn', errstr)
                    warnings.append(errstr)
                    continue

                else:
                    evt.eventType = "onBackupEnd"
                    evt.fields = {
                        'serviceName': srvTarget['name'],
                        'backupServer': cfg['target']['server'],
                        'resourceName': srvTarget['root'],
                        'backupSuccess': True,
                    }

                    evt.push()

                    __afterSuccessBackup(tgtPath2, evt, srvTarget, srvTarget['root'])
                    blMsg('info', "Backup uploaded, " + srvTarget['root'])
                    continue


    # backup completed!
    evt.eventType = "onAppEnd"

    if len(warnings) > 0:
        evt.fields = {
            'exitStatus': 'warnings',
            'warnings': warnings
        }
    else:
        evt.fields = {
            'exitStatus': 'ok'
        }
    

    evt.push()
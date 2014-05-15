# Functions to install files on all nodes. 

import os
import glob
import re

import util
import cmdoutput
import execute
import config

# In all paths given in this file, ${<option>} will replaced with the value of
# the corresponding configuration option.

# Directories/files in form (path, mirror) which are synced from the manager to
# all nodes.
# If 'mirror' is true, the path is fully mirrored recursively, otherwise the
# directory is just created.
Syncs = [
    ("${brobase}", False),
    ("${brobase}/share", True),
    ("${cfgdir}", True),
    ("${libdir}", True),
    ("${bindir}", True),
    ("${policydirsiteinstall}", True),
    ("${policydirsiteinstallauto}", True),
    # ("${policydir}", True),
    # ("${staticdir}", True),
    ("${logdir}", False),
    ("${spooldir}", False),
    ("${tmpdir}", False),
    ("${broctlconfigdir}/broctl-config.sh", True)
    ]

# In NFS-mode, only these will be synced.
NFSSyncs = [
    ("${policydirsiteinstall}", True),
    ("${policydirsiteinstallauto}", True),
    ("${broctlconfigdir}/broctl-config.sh", True)
    ]

# Generate shell script that sets Broctl dynamic variables according
# to their current values.  This shell script gets included in all
# other scripts.
def generateDynamicVariableScript(cmdout):
    cfg_path = os.path.join(config.Config.broctlconfigdir, "broctl-config.sh")
    cfg_file = open(cfg_path, 'w')
    for substvartuple in config.Config.options():
        substvar = substvartuple[0]
        # don't write out if it has an invalid bash variable name
        if not re.search("-", substvar):
            substvarvalue = substvartuple[1]
            cfg_file.write("%s=\"%s\"\n" % (substvar.replace(".", "_"), substvarvalue))
    cfg_file.close()

    symlink = os.path.join(config.Config.scriptsdir, "broctl-config.sh")

    try:
        if os.readlink(symlink) != cfg_path:
            # attempt to update the symlink
            try:
                util.force_symlink(cfg_path, symlink)
            except OSError, e:
                cmdout.error("failed to update symlink '%s' to point to '%s': %s" % (symlink, cfg_path, e.strerror))
                return False
    except OSError, e:
        cmdout.error("failed to resolve symlink '%s': %s" % (symlink, e.strerror))
        return False

    return True

# Performs the complete broctl installation process.
#
# If local_only is True, nothing is propagated to other nodes.
def install(local_only):
    cmdout = cmdoutput.CommandOutput()
    cmdSuccess = True

    if not config.Config.determineBroVersion(cmdout):
        cmdSuccess = False
        return (cmdSuccess, cmdout)

    manager = config.Config.manager()

    # Delete previously installed policy files to not mix things up.
    policies = [config.Config.policydirsiteinstall, config.Config.policydirsiteinstallauto]

    for p in policies:
        if os.path.isdir(p):
            cmdout.info("removing old policies in %s ..." % p)
            if not execute.rmdir(manager, p, cmdout):
                cmdSuccess = False

    cmdout.info("creating policy directories ...")
    for p in policies:
        if not execute.mkdir(manager, p, cmdout):
            cmdSuccess = False

    # Install local site policy.

    if config.Config.sitepolicypath:
        cmdout.info("installing site policies ...")
        dst = config.Config.policydirsiteinstall
        for dir in config.Config.sitepolicypath.split(":"):
            dir = config.Config.subst(dir)
            for file in glob.glob(os.path.join(dir, "*")):
                if not execute.install(manager, file, dst, cmdout):
                    cmdSuccess = False

    makeLayout(config.Config.policydirsiteinstallauto, cmdout)
    if not makeLocalNetworks(config.Config.policydirsiteinstallauto, cmdout):
        cmdSuccess = False
    makeConfig(config.Config.policydirsiteinstallauto, cmdout)

    current = config.Config.subst(os.path.join(config.Config.logdir, "current"))
    try:
        util.force_symlink(manager.cwd(), current)
    except (IOError, OSError), e:
        cmdSuccess = False
        cmdout.error("failed to update current log symlink")
        return (cmdSuccess, cmdout)

    if not generateDynamicVariableScript(cmdout):
        cmdSuccess = False
        return (cmdSuccess, cmdout)

    if local_only:
        return (cmdSuccess, cmdout)

    # Sync to clients.
    cmdout.info("updating nodes ...")

    nodes = []

    # Make sure we install each remote host only once.
    for n in config.Config.hosts():
        if execute.isLocal(n, cmdout):
            continue

        if not execute.isAlive(n.addr, cmdout):
            cmdSuccess = False
            continue

        nodes += [n]

    if config.Config.havenfs != "1":
        # Non-NFS, need to explicitly synchronize.
        dirs = []
        for dir in [config.Config.subst(dir) for (dir, mirror) in Syncs if not mirror]:
            dirs += [(n, dir) for n in nodes]

        for (node, success) in execute.mkdirs(dirs, cmdout):
            if not success:
                cmdout.error("cannot create directory %s on %s" % (dir, node.name))
                cmdSuccess = False

        paths = [config.Config.subst(dir) for (dir, mirror) in Syncs if mirror]
        if not execute.sync(nodes, paths, cmdout):
            cmdSuccess = False

    else:
        # NFS. We only need to take care of the spool/log directories.
        paths = [config.Config.spooldir]
        paths += [config.Config.tmpdir]

        dirs = []
        for dir in paths:
            dirs += [(n, dir) for n in nodes]

        for dir in [config.Config.subst(dir) for (dir, mirror) in NFSSyncs if not mirror]:
            dirs += [(n, dir) for n in nodes]

        # We need this only on the manager.
        dirs += [(manager, config.Config.logdir)]

        for (node, success) in execute.mkdirs(dirs, cmdout):
            if not success:
                cmdout.error("cannot create (some of the) directories %s on %s" % (",".join(paths), node.name))
                cmdSuccess = False

        paths = [config.Config.subst(dir) for (dir, mirror) in NFSSyncs if mirror]
        if not execute.sync(nodes, paths, cmdout):
            cmdSuccess = False

    # Save current node configuration state.
    config.Config.updateNodeCfgHash()

    # Save current configuration state.
    config.Config.updateBroctlCfgHash()

    return (cmdSuccess, cmdout)

# Create Bro-side broctl configuration broctl-layout.bro.
def makeLayout(path, cmdout, silent=False):
    class port:
        def __init__(self, startport):
            self.p = startport

        def nextPort(self, node):
            self.p += 1
            node.setPort(self.p)
            return self.p

    manager = config.Config.manager()

    if not manager:
        return

    broport = port(int(config.Config.broport) - 1)

    filename = os.path.join(path, "cluster-layout.bro")

    # If there is a standalone node, delete any cluster-layout file to
    # avoid the cluster framework from activating and get out of here.
    if config.Config.nodes("standalone"):
        if os.access(filename, os.W_OK):
            os.unlink(filename)
        # We do need to establish the port for the manager.
        if not silent:
            cmdout.info("generating standalone-layout.bro ...")

        filename = os.path.join(path, "standalone-layout.bro")
        out = open(filename, "w")
        print >>out, "# Automatically generated. Do not edit."
        # This is the port that standalone nodes listen on for remote control by default.
        print >>out, "redef Communication::listen_port = %s/tcp;" % broport.nextPort(manager)
        print >>out, "redef Communication::nodes += {"
        print >>out, "	[\"control\"] = [$host=%s, $zone_id=\"%s\", $class=\"control\", $events=Control::controller_events]," % (util.formatBroAddr(manager.addr), manager.zone_id)
        print >>out, "};"
        out.close()

    else:
        if not silent:
            cmdout.info("generating cluster-layout.bro ...")

        out = open(filename, "w")

        workers = config.Config.nodes("workers")
        proxies = config.Config.nodes("proxies")

        print >>out, "# Automatically generated. Do not edit."
        print >>out, "redef Cluster::nodes = {"

        # Control definition.  For now just reuse the manager information.
        print >>out, "\t[\"control\"] = [$node_type=Cluster::CONTROL, $ip=%s, $zone_id=\"%s\", $p=%s/tcp]," % (util.formatBroAddr(manager.addr), config.Config.zoneid, broport.nextPort(manager))

        # Manager definition
        print >>out, "\t[\"%s\"] = [$node_type=Cluster::MANAGER, $ip=%s, $zone_id=\"%s\", $p=%s/tcp, $workers=set(" % (manager.name, util.formatBroAddr(manager.addr), manager.zone_id, broport.nextPort(manager)),
        for s in workers:
            print >>out, "\"%s\"" % (s.name),
            if s != workers[-1]:
                print >>out, ",",
        print >>out, ")],"

        # Proxies definition
        for p in proxies:
            print >>out, "\t[\"%s\"] = [$node_type=Cluster::PROXY, $ip=%s, $zone_id=\"%s\", $p=%s/tcp, $manager=\"%s\", $workers=set(" % (p.name, util.formatBroAddr(p.addr), p.zone_id, broport.nextPort(p), manager.name),
            for s in workers:
                print >>out, "\"%s\"" % (s.name),
                if s != workers[-1]:
                    print >>out, ",",
            print >>out, ")],"

        # Workers definition
        for w in workers:
            p = w.count % len(proxies)
            print >>out, "\t[\"%s\"] = [$node_type=Cluster::WORKER, $ip=%s, $zone_id=\"%s\", $p=%s/tcp, $interface=\"%s\", $manager=\"%s\", $proxy=\"%s\"]," % (w.name, util.formatBroAddr(w.addr), w.zone_id, broport.nextPort(w), w.interface, manager.name, proxies[p].name)

        # Activate time-machine support if configured.
        if config.Config.timemachinehost:
            print >>out, "\t[\"time-machine\"] = [$node_type=Cluster::TIME_MACHINE, $ip=%s, $p=%s]," % (config.Config.timemachinehost, config.Config.timemachineport)

        print >>out, "};"

        out.close()

# Reads in a list of networks from file.
def readNetworks(file):

    nets = []

    for line in open(file):
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        fields = line.split(None, 1)

        cidr = util.formatBroPrefix(fields[0])
        tag = ""
        if len(fields) == 2:
            tag = fields[1]

        nets += [(cidr, tag)]

    return nets


# Create Bro script which contains a list of local networks.
def makeLocalNetworks(path, cmdout, silent=False):

    netcfg = config.Config.localnetscfg

    if not os.path.exists(netcfg):
        cmdout.error("list of local networks does not exist in %s" % netcfg)
        return False

    if not silent:
        cmdout.info("generating local-networks.bro ...")

    nets = readNetworks(netcfg)

    out = open(os.path.join(path, "local-networks.bro"), "w")
    print >>out, "# Automatically generated. Do not edit.\n"

    print >>out, "redef Site::local_nets = {"
    for (cidr, tag) in nets:
        print >>out, "\t%s," % cidr,
        if tag:
            print >>out, "\t# %s" % tag,
        print >>out
    print >>out, "};\n"
    out.close()

    return True


def makeConfig(path, cmdout, silent=False):
    manager = config.Config.manager()

    if not manager:
        return

    if not silent:
        cmdout.info("generating broctl-config.bro ...")

    filename = os.path.join(path, "broctl-config.bro")
    out = open(filename, "w")
    print >>out, "# Automatically generated. Do not edit."
    print >>out, "redef Notice::mail_dest = \"%s\";" % config.Config.mailto
    print >>out, "redef Notice::mail_dest_pretty_printed = \"%s\";" % config.Config.mailalarmsto
    print >>out, "redef Notice::sendmail  = \"%s\";" % config.Config.sendmail
    print >>out, "redef Notice::mail_subject_prefix  = \"%s\";" % config.Config.mailsubjectprefix
    print >>out, "redef Notice::mail_from  = \"%s\";" % config.Config.mailfrom
    if manager.type != "standalone":
        print >>out, "@if ( Cluster::local_node_type() == Cluster::MANAGER )"
    print >>out, "redef Log::default_rotation_interval = %s secs;" % config.Config.logrotationinterval
    print >>out, "redef Log::default_mail_alarms_interval = %s secs;" % config.Config.mailalarmsinterval
    if manager.type != "standalone":
        print >>out, "@endif"
    if config.Config.ipv6comm == "1":
        print >>out, "redef Communication::listen_ipv6 = T ;"
    else:
        print >>out, "redef Communication::listen_ipv6 = F ;"

    out.close()


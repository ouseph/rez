"""
rez-release

A tool for releasing rez - compatible projects centrally
"""

import sys
import os
import os.path
import shutil
import inspect
import time
import subprocess
import smtplib
from email.mime.text import MIMEText

from rez_metafile import *
import versions

##############################################################################
# Globals
##############################################################################

_release_classes = []

##############################################################################
# Exceptions
##############################################################################

class RezReleaseError(Exception):
	def __init__(self, value):
		self.value = value
	def __str__(self):
		return str(self.value)

class RezReleaseUnsupportedMode(RezReleaseError):
	"""
	Raise this error during initialization of a RezReleaseMode sub-class to indicate
	that the mode is unsupported in the given context
	"""
	pass

##############################################################################
# Constants
##############################################################################

REZ_RELEASE_PATH_ENV_VAR = 		"REZ_RELEASE_PACKAGES_PATH"
EDITOR_ENV_VAR		 	= 		"REZ_RELEASE_EDITOR"
RELEASE_COMMIT_FILE 	= 		"rez-release-commit.tmp"


##############################################################################
# Public Functions
##############################################################################

def register_release_mode(name, cls):
	"""
	Register a subclass of RezReleaseMode for performing a custom release procedure.
	"""
	assert inspect.isclass(cls) and issubclass(cls, RezReleaseMode), \
		"Provided class is not a subclass of RezReleaseMode"
	assert name not in list_release_modes(), \
		"Mode has already been registered"
	# put new entries at the front
	_release_classes.insert(0, (name, cls))

def list_release_modes():
	return [name for (name, cls) in _release_classes]

def list_available_release_modes(path):
	modes = []
	for name, cls in _release_classes:
		try:
			cls(path)
		except:
			pass
		else:
			modes.append(name)
	return modes

def release_from_path(path, commit_message, njobs, build_time, allow_not_latest,
					  mode='svn'):
	"""
	release a package from the given path on disk, copying to the relevant tag,
	and performing a fresh build before installing it centrally. 

	path:
		filepath containing the project to be released
	commit_message:
		None, or message string to write to svn, along with changelog. 
		If 'commit_message' None, the user will be prompted for input using the
		editor specified by $REZ_RELEASE_EDITOR.
	njobs:
		number of threads to build with; passed to make via -j flag
	build_time:
		epoch time to build at. If 0, use current time
	allow_not_latest:
		if True, allows for releasing a tag that is not > the latest tag version
	"""
	cls = dict(_release_classes)[mode]
	rel = cls(path)
	rel.release(commit_message, njobs, build_time, allow_not_latest)


##############################################################################
# Utilities
##############################################################################

def _expand_path(path):
	return os.path.abspath(os.path.expandvars(os.path.expanduser(path)))

def remove_write_perms(path):
	import stat
	st = os.stat(path)
	mode = st.st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
	os.chmod(path, mode)

def copytree(src, dst, symlinks=False, ignore=None):
	'''
	copytree that supports hard-linking
	'''
	print "copying directory", src
	names = os.listdir(src)
	if ignore is not None:
		ignored_names = ignore(src, names)
	else:
		ignored_names = set()

	os.makedirs(dst)
	errors = []
	for name in names:
		if name in ignored_names:
			continue
		srcname = os.path.join(src, name)
		dstname = os.path.join(dst, name)
		try:
			if symlinks and os.path.islink(srcname):
				linkto = os.readlink(srcname)
				os.symlink(linkto, dstname)
			elif os.path.isdir(srcname):
				copytree(srcname, dstname, symlinks, ignore)
			else:
				#shutil.copy2(srcname, dstname)
				os.link(srcname, dstname)
		# XXX What about devices, sockets etc.?
		except (IOError, os.error) as why:
			errors.append((srcname, dstname, str(why)))
		# catch the Error from the recursive copytree so that we can
		# continue with other files
		except shutil.Error as err:
			errors.extend(err.args[0])
	try:
		shutil.copystat(src, dst)
	except shutil.WindowsError:
		# can't copy file access times on Windows
		pass
	except OSError as why:
		errors.extend((src, dst, str(why)))
	if errors:
		raise shutil.Error(errors)

def send_release_email(subject, body):
	from_ = os.getenv("REZ_RELEASE_EMAIL_FROM", "rez")
	to_ = os.getenv("REZ_RELEASE_EMAIL_TO")
	if not to_:
		return
	recipients = to_.replace(':',' ').replace(';',' ').replace(',',' ')
	recipients = recipients.strip().split()
	if not recipients:
		return

	print
	print("---------------------------------------------------------")
	print("rez-release: sending notification emails...")
	print("---------------------------------------------------------")
	print
	print "sending to:\n%s" % str('\n').join(recipients)

	smtphost = os.getenv("REZ_RELEASE_EMAIL_SMTP_HOST", "localhost")
	smtpport = os.getenv("REZ_RELEASE_EMAIL_SMTP_PORT")

	msg = MIMEText(body)
	msg["Subject"] = subject
	msg["From"] = from_
	msg["To"] = str(',').join(recipients)

	try:
		s = smtplib.SMTP(smtphost, smtpport)
		s.sendmail(from_, recipients, msg.as_string())
		print 'email(s) sent.'
	except Exception as e:
		print  >> sys.stderr, "Emailing failed: %s" % str(e)

##############################################################################
# Implementation Classes
##############################################################################

class RezReleaseMode(object):
	'''
	Base class for all release modes.

	A release mode typically corresponds to a particular version control system
	(VCS), such as svn, git, or mercurial (hg). 

	The base implementation allows for release without the use of any version
	control system.

	To implement a new mode, start by creating a subclass overrides to the
	high level methods:
		- validate_repostate
		- create_release_tag
		- get_tags
		- get_tag_meta_str
		- copy_source

	If you need more control, you can also override the lower level methods that
	correspond to the release phases:
		- pre_build
		- build
		- install
		- post_install
	'''
	def __init__(self, path):
		self.path = _expand_path(path)
		
		# variables filled out in pre_build()
		self.metadata = None
		self.base_dir = None
		self.pkg_release_dir = None
		self.package_uuid_exists = None
		self.changelog_file = os.path.abspath('build/rez-release-changelog.txt')
		self.editor = None
		# for cached property: False indicates it has not been cached
		# (since it may be None after caching)
		self._last_tagged_version = False

	def release(self, commit_message, njobs, build_time, allow_not_latest):
		'''
		Main entry point for executing the release
		'''
		self.commit_message = commit_message
		self.njobs = njobs
		self.build_time = build_time
		self.allow_not_latest = allow_not_latest

		self.pre_build()
		self.build()
		self.install()
		self.post_install()

	def get_metadata(self):
		'''
		return a ConfigMetadata instance for this project's package.yaml file.
		'''
		# check for ./package.yaml
		yaml_path = os.path.join(self.path, "package.yaml")
		if not os.access(yaml_path, os.F_OK):
			raise RezReleaseError(yaml_path + " not found")

		# load the package metadata
		metadata = ConfigMetadata(yaml_path + "")
		if (not metadata.version):
			raise RezReleaseError(yaml_path + " does not specify a version")
		try:
			self.this_version = versions.Version(metadata.version)
		except versions.VersionError:
			raise RezReleaseError(yaml_path + " contains illegal version number")

		# metadata must have name
		if not metadata.name:
			raise RezReleaseError(yaml_path + " is missing name")

		# metadata must have uuid
		if not metadata.uuid:
			raise RezReleaseError(yaml_path + " is missing uuid")

		# .metadata must have description
		if not metadata.description:
			raise RezReleaseError(yaml_path + " is missing a description")

		# metadata must have authors
		if not metadata.authors:
			raise RezReleaseError(yaml_path + " is missing authors")

		return metadata

	# utilities  ---------
	def _write_changelog(self):
		changelog = self.get_changelog()
		if changelog:
			if self.commit_message:
				self.commit_message += '\n' + changelog
			else:
				# prompt for tag comment, automatically setting to the change-log
				self.commit_message = "\n\n" + changelog
	
			# write the changelog to file, so that rez-build can install it as metadata
			chlogf = open(self.changelog_file, 'w')
			chlogf.write(changelog)
			chlogf.close()

	def _get_commit_message(self):
		'''
		Prompt user for a commit message using the editor specified by
		$REZ_RELEASE_EDITOR.
		
		The starting value of the editor will be the message passed on the
		command-line, if given.
		'''
		
		tmpf = os.path.join(self.base_dir, RELEASE_COMMIT_FILE)
		f = open(tmpf, 'w')
		f.write(self.commit_message)
		f.close()

		try:
			returncode = subprocess.call(self.editor + ' ' + tmpf, shell=True)
			if returncode == 0:
				print "Got commit message"
				# if commit file was unchanged, then give a chance to abort the release
				new_commit_message = open(tmpf).read()
				if (new_commit_message == self.commit_message):
					try:
						reply = raw_input("Commit message unchanged - (a)bort or (c)ontinue? ")
						if reply != 'c':
							sys.exit(1)
					except EOFError:
						# raw_input raises EOFError on Ctl-D (Unix) and Ctl-Z+Return (Windows)
						sys.exit(1)
				self.commit_message = new_commit_message
			else:
				raise RezReleaseError("Error getting commit message")
		finally:
			# always remove the temp file
			os.remove(tmpf)

	def check_uuid(self, package_uuid_file):
		'''
		check uuid against central uuid for this package family, to ensure that
		we are not releasing over the top of a totally different package due to
		naming clash
		'''
		try:
			existing_uuid = open(package_uuid_file).read().strip()
		except Exception:
			package_uuid_exists = False
			existing_uuid = self.metadata.uuid
		else:
			package_uuid_exists = True

		if existing_uuid != self.metadata.uuid:
			raise RezReleaseError("the uuid in '" + package_uuid_file +
				"' does not match this package's uuid - you may have a package "
				"name clash. All package names must be unique.")
		return package_uuid_exists
	
	def write_time_metafile(self):
		# the very last thing we do is write out the current date-time to a metafile. This is
		# used by rez to specify when a package 'officially' comes into existence.
		time_metafile = os.path.join(self.pkg_release_dir, self.metadata.version,
									'.metadata' , 'release_time.txt')
		timef = open(time_metafile, 'w')
		time_epoch = int(time.mktime(time.localtime()))
		timef.write(str(time_epoch) + '\n')
		timef.close()

	def send_email(self):
		usr = os.getenv("USER", "unknown.user")
		pkgname = "%s-%s" % (self.metadata.name, str(self.this_version))
		subject = "[rez] [release] %s released %s" % (usr, pkgname)
		if len(self.variants) > 1:
			subject += " (%d variants)" % len(self.variants)
		send_release_email(subject, self.commit_message)

	def check_installed_variant(self, instpath):
		for root, dirs, files in os.walk(instpath):
			has_py = False
			for name in files:
				path = os.path.join(root, name)
				# remove any .pyc files that may have been spawned
				if name.endswith('.pyc'):
					os.remove(path)
				elif not name == '.metadata':
					if name.endswith('.py'):
						has_py = True
					# Remove write permissions from all installed files.
					remove_write_perms(path)
			# Remove write permissions on dirs that contain py files
			if has_py:
				remove_write_perms(root)

	# VCS and tagging ---------
	def create_release_tag(self):
		'''
		On release, it is customary for a VCS to generate a tag
		'''
		pass

	def get_tags(self):
		'''
		Return a list of tags for this VCS
		'''
		return []

	def get_tag_meta_str(self):
		'''
		Return a tag identifier string for this VCS.
		Could be a url, revision, hash, etc.
		Cannot contain spaces, dashes, or newlines.
		'''
		return self.tag_url

	@property
	def last_tagged_version(self):
		'''
		Cached property for the last tagged version.  None if there are no tags.
		'''
		if self._last_tagged_version is False:
			# False means the value has not been cached yet
			self._last_tagged_version = self.get_last_tagged_version()
		return self._last_tagged_version

	def get_last_tagged_version(self):
		'''
		Find the latest tag returned by self.get_tags() or None if there are
		no tags.
		'''
		latest_ver = versions.Version("0")

		found_tag = False
		for tag in self.get_tags():
			try:
				ver = versions.Version(tag)
			except Exception:
				continue
			if ver > latest_ver:
				latest_ver = ver
				found_tag = True
		
		if not found_tag:
			return
		return latest_ver

	def validate_version(self):
		'''
		validate the version being released, by ensuring it is greater than the
		latest existing tag, as provided by self.last_tagged_version property.

		Ignored if allow_not_latest is True.
		'''
		if self.allow_not_latest:
			return

		if self.last_tagged_version is None:
			return

		last_tag_str = str(self.last_tagged_version)
		if last_tag_str[0] == 'v':
			# old style
			return

		# FIXME: is the tag put under version control really our most reliable source
		# for previous released versions? Can't we query the versions of our package
		# on $REZ_RELEASE_PACKAGES_PATH?
		if self.this_version <= self.last_tagged_version:
			raise RezReleaseError("cannot release: current version '" + self.metadata.version + \
				"' is not greater than the latest tag '" + last_tag_str + \
				"'. Version up or pass --allow-not-latest.")

	def validate_repostate(self):
		'''
		ensure that the VCS working copy is up-to-date
		'''
		pass

	def copy_source(self, build_dir):
		'''
		Copy the source to the build directory.

		This is particularly useful for revision control systems, which can
		export a clean unmodified copy
		'''
		def ignore(src, names):
			'''
			returns a list of names to ignore, given the current list
			'''
			if src == self.base_dir:
				return names
			return [x for x in names if x.startswith('.')]

		copytree(os.getcwd(), build_dir, symlinks=True,
				ignore=ignore)

	def get_changelog(self):
		'''
		get the changelog text since the last release
		'''
		return ''

	def get_build_cmd(self, vararg):
		tag_meta_str = self.get_tag_meta_str()
		if tag_meta_str: 
			tag_meta_str = " -s " + tag_meta_str
		else:
			tag_meta_str = ''

		if os.path.exists(self.changelog_file):
			changelog = " -c " + self.changelog_file
		else:
			changelog = ''

		build_cmd = "rez-build" + \
			" -t " + str(self.build_time) + \
			" " + vararg + \
			tag_meta_str + \
			changelog + \
			" -- -- -j" + str(self.njobs)
		return build_cmd

	def get_install_cmd(self, vararg):
		tag_meta_str = self.get_tag_meta_str()
		if tag_meta_str: 
			tag_meta_str = " -s " + tag_meta_str
		else:
			tag_meta_str = ''

		if os.path.exists(self.changelog_file):
			changelog = " -c " + self.changelog_file
		else:
			changelog = ''

		build_cmd = "rez-build -n" + \
			" -t " + str(self.build_time) + \
			" " + vararg + \
			tag_meta_str + \
			changelog + \
			" -- -c -- install"
		return build_cmd

	# phases ---------
	def pre_build(self):
		'''
		Fill out variables and check for problems
		'''
		self.metadata = self.get_metadata()

		self.pkg_release_dir = os.getenv(REZ_RELEASE_PATH_ENV_VAR)
		if not self.pkg_release_dir:
			raise RezReleaseError("$" + REZ_RELEASE_PATH_ENV_VAR + " is not set.")

		self.pkg_release_dir = os.path.join(self.pkg_release_dir, self.metadata.name)
		self.package_uuid_file = os.path.join(self.pkg_release_dir,  "package.uuid")

		self.package_uuid_exists = self.check_uuid(self.package_uuid_file)

		self.variants = self.metadata.get_variants()
		if not self.variants:
			self.variants = [ None ]

		# create base dir to do clean builds from
		self.base_dir = os.path.join(os.getcwd(), "build", "rez-release")
		if os.path.exists(self.base_dir):
			if os.path.islink(self.base_dir):
				os.remove(self.base_dir)
			elif os.path.isdir(self.base_dir):
				shutil.rmtree(self.base_dir)
			else:
				os.remove(self.base_dir)

		os.makedirs(self.base_dir)

		# take note of the current time, and use it as the build time for all
		# variants. This ensures that all variants will find the same packages, in
		# case some new packages are released during the build.
		if str(self.build_time) == "0":
			self.build_time = str(int(time.time()))

		if (self.commit_message is None):
			# get preferred editor for commit message
			self.editor = os.getenv(EDITOR_ENV_VAR)
			if not self.editor:
				raise RezReleaseError("rez-release: $" + EDITOR_ENV_VAR + " is not set.")
			self.commit_message = ''

		# check we're in a state to release (no modified/out-of-date files etc)
		self.validate_repostate()

		self.validate_version()

		self._write_changelog()

		self._get_commit_message()

	def build(self):
		'''
		Perform build of all variants
		'''
		# svn-export each variant out to a clean directory, and build it locally. If any
		# builds fail then this release is aborted

		print
		print("---------------------------------------------------------")
		print("rez-release: building...")
		print("---------------------------------------------------------")

		for varnum, variant in enumerate(self.variants):
			self.build_variant(variant, varnum)

	def build_variant(self, variant, varnum):
		'''
		Build a single variant
		'''
		if variant:
			varname = "project variant #" + str(varnum)
			vararg = "-v " + str(varnum)
			subdir = os.path.join(self.base_dir, str(varnum))
		else:
			varnum = ''
			varname = "project"
			vararg = ''
			subdir = self.base_dir
		print
		print("rez-release: creating clean copy of " + varname + " to " + subdir + "...")

		if os.path.exists(subdir):
			shutil.rmtree(subdir)

		self.copy_source(subdir)

		# build it
		build_cmd = self.get_build_cmd(vararg)

		print
		print("rez-release: building " + varname + " in " + subdir + "...")
		print("rez-release: invoking: " + build_cmd)

		build_cmd = "cd " + subdir + " ; " + build_cmd
		pret = subprocess.Popen(build_cmd, shell=True)
		pret.communicate()
		if (pret.returncode != 0):
			raise RezReleaseError("rez-release: build failed")

	def install(self):
		'''
		Perform installation of all variants
		'''
		# now install the variants
		print
		print("---------------------------------------------------------")
		print("rez-release: installing...")
		print("---------------------------------------------------------")

		# create the package.uuid file, if it doesn't exist
		if not self.package_uuid_exists:
			os.makedirs(self.pkg_release_dir)

			f = open(self.package_uuid_file, 'w')
			f.write(self.metadata.uuid)
			f.close()

		# install the variants
		for varnum, variant in enumerate(self.variants):
			self.install_variant(variant, varnum)

	def install_variant(self, variant, varnum):
		'''
		Install a single variant
		'''
		if variant:
			varname = "project variant #" + str(varnum)
			vararg = "-v " + str(varnum)
		else:
			varnum = ''
			varname = 'project'
			vararg = ''
		subdir = self.base_dir + '/' + str(varnum) + '/'

		# determine install self.path
		pret = subprocess.Popen("cd " + subdir + " ; rez-build -i " + vararg, \
			stdout=subprocess.PIPE, shell=True)
		instpath, instpath_err = pret.communicate()
		if (pret.returncode != 0):
			raise RezReleaseError("rez-release: install failed!! A partial central installation may " + \
				"have resulted, please see to this immediately - it should probably be removed.")
		instpath = instpath.strip()

		print
		print("rez-release: installing " + varname + " from " + subdir + " to " + instpath + "...")

		# run rez-build, and:
		# * manually specify the svn-url to write into self.metadata;
		# * manually specify the changelog file to use
		# these steps are needed because the code we're building has been svn-exported, thus
		# we don't have any svn context.

		# TODO: rewrite all of this using pure python:

		build_cmd = self.get_install_cmd(vararg)
		pret = subprocess.Popen("cd " + subdir + " ; " + build_cmd, shell=True)

		pret.wait()
		if (pret.returncode != 0):
			raise RezReleaseError("rez-release: install failed!! A partial "
								  "central installation may have resulted, "
								  "please see to this immediately - "
								  "it should probably be removed.")

		self.check_installed_variant(instpath)

	def post_install(self):
		'''
		Final stage after installation
		'''
		self.write_time_metafile()

		self.send_email()

		print
		print("---------------------------------------------------------")
		print("rez-release: tagging...")
		print("---------------------------------------------------------")
		print

		self.create_release_tag()

		print
		print("rez-release: your package was released successfully.")
		print

register_release_mode('base', RezReleaseMode)

##############################################################################
# Subversion
##############################################################################

class SvnValueCallback:
	"""
	simple functor class
	"""
	def __init__(self, value):
		self.value = value
	def __call__(self):
		return True, self.value

# TODO: remove these functions once everything is consolidated onto the SvnRezReleaseMode class

def svn_get_client():
	import pysvn
	# check we're in an svn working copy
	client = pysvn.Client()
	client.set_interactive(True)
	client.set_auth_cache(False)
	client.set_store_passwords(False)
	client.callback_get_login = getSvnLogin
	return client

def svn_url_exists(client, url):
	"""
	return True if the svn url exists
	"""
	import pysvn
	try:
		svnlist = client.info2(url, recurse = False)
		return len( svnlist ) > 0
	except pysvn.ClientError:
		return False

def get_last_changed_revision(client, url):
	"""
	util func, get last revision of url
	"""
	import pysvn
	try:
		svn_entries = client.info2(url,
								pysvn.Revision(pysvn.opt_revision_kind.head),
								recurse=False)
		if len(svn_entries) == 0:
			raise RezReleaseError("svn.info2() returned no results on url '" + url + "'")
		return svn_entries[0][1].last_changed_rev
	except pysvn.ClientError, ce:
		raise RezReleaseError("svn.info2() raised ClientError: %s"%ce)

def getSvnLogin(realm, username, may_save):
	"""
	provide svn with permissions. @TODO this will have to be updated to take
	into account automated releases etc.
	"""
	import getpass

	print "svn requires a password for the user '" + username + "':"
	pwd = ''
	while(pwd.strip() == ''):
		pwd = getpass.getpass("--> ")

	return True, username, pwd, False

class SvnRezReleaseMode(RezReleaseMode):
	def __init__(self, path):
		super(SvnRezReleaseMode, self).__init__(path)

		self.svnc = svn_get_client()

		svn_entry = self.svnc.info(self.path)
		if not svn_entry:
			raise RezReleaseUnsupportedMode("'" + self.path + "' is not an svn working copy")
		self.this_url = str(svn_entry["url"])

		# variables filled out in pre_build()
		self.tag_url = None

	def get_tag_url(self, version=None):
		# find the base path, ie where 'trunk', 'branches', 'tags' should be
		pos_tr = self.this_url.find("/trunk")
		pos_br = self.this_url.find("/branches")
		pos = max(pos_tr, pos_br)
		if (pos == -1):
			raise RezReleaseError(self.path + "is not in a branch or trunk")
		base_url = self.this_url[:pos]
		tag_url = base_url + "/tags"

		if version:
			tag_url += '/' + str(version)
		return tag_url

	def svn_url_exists(self, url):
		return svn_url_exists(self.svnc, url)

	def get_last_tagged_revision(self):
		'''
		Return the revision number and tag url of the last tag
		'''
		tag_url = self.get_tag_url()
		latest_tag_url = tag_url + '/' + str(self.last_tagged_version)
		latest_rev = get_last_changed_revision(self.svnc, latest_tag_url)
		
		return latest_rev.number, latest_tag_url

	# Overrides ------
	def get_tags(self):
		tag_url = self.get_tag_url()

		if not self.svn_url_exists(tag_url):
			raise RezReleaseError("Tag url does not exist: " + tag_url)

		# read all the tags (if any) and find the most recent
		tags = self.svnc.ls(tag_url)
		if len(tags) == 0:
			raise RezReleaseError("No existing tags")

		tags = []
		for tag_entry in tags:
			tag = tag_entry["name"].split('/')[-1]
			if tag[0] == 'v':
				# old launcher-style vXX_XX_XX
				nums = tag[1:].split('_')
				tag = str(int(nums[0])) + '.' + str(int(nums[1])) + '.' + str(int(nums[2]))
			tags.append(tag)
		return tags

	def get_last_tagged_version(self):
		"""
		returns a rez Version
		"""
		if '/branches/' in self.this_url:
			# create a Version instance from the branch we are on this makes sure it's
			# a Well Formed Version, and also puts the base version in 'latest_ver'
			latest_ver = versions.Version(self.this_url.split('/')[-1])
		else:
			latest_ver = versions.Version("0")

		found_tag = False
		for tag in self.get_tags():
			try:
				ver = versions.Version(tag)
			except Exception:
				continue
		
			if ver > latest_ver:
				latest_ver = ver
				found_tag = True
		
		if not found_tag:
			return
		return latest_ver

	def validate_version(self):
		self.tag_url = self.get_tag_url(self.version)
		# check that this tag does not already exist
		if self.svn_url_exists(self.tag_url):
			raise RezReleaseError("cannot release: the tag '"
				+ self.tag_url + "' already exists in svn." + \
				" You may need to up your version, svn-checkin and try again.")

		super(SvnRezReleaseMode, self).validate_version()

	def validate_repostate(self):
		status_list = self.svnc.status(self.path, get_all=False, update=True)
		status_list_known = []
		for status in status_list:
			if status.entry:
				status_list_known.append(status)
		if len(status_list_known) > 0:
			raise RezReleaseError("'" + self.path + "' is not in a state to "
								  "release - you may need to "
								  "svn-checkin and/or svn-update: " +
								   str(status_list_known))

		# do an update
		print("rez-release: svn-updating...")
		self.svnc.update(self.path)

	def create_release_tag(self):
		# at this point all variants have built and installed successfully. Copy to the new tag
		print("rez-release: creating project tag in: " + self.tag_url + "...")
		self.svnc.callback_get_log_message = SvnValueCallback(self.commit_message)

		self.svnc.copy2([(self.this_url,)], self.tag_url, make_parents=True)

	def get_metadata(self):
		result = super(SvnRezReleaseMode, self).get_metadata()
		# check that ./package.yaml is under svn control
		if not self.svn_url_exists(self.this_url + "/package.yaml"):
			raise RezReleaseError(self.path + "/package.yaml is not under source control")
		return result

	def get_tag_meta_str(self):
		return self.tag_url

	def copy_source(self, build_dir):
		# svn-export it. pysvn is giving me some false assertion crap on 'is_canonical(self.path)' here, hence shell
		pret = subprocess.Popen(["svn", "export", self.this_url, build_dir])
		pret.communicate()
		if (pret.returncode != 0):
			raise RezReleaseError("rez-release: svn export failed")

	def get_changelog(self):
		# Get the changelog.
		# TODO: read this in directly using pysvn and the latest tag
# 		log = ''
# 		try:
# 			result = self.get_last_tagged_revision()
# 		except (ImportError, RezReleaseError):
# 			log = "Changelog since first revision, tag:(NONE)"
# 			#log += svn log -r HEAD:1 --stop-on-copy
# 		else:
# 			if result is None:
# 				log = "Changelog since first branch revision:(NONE)"
# 				#log += svn log -r HEAD:1 --stop-on-copy
# 			else:
# 				rev, tagurl = result
# 				log = "Changelog since rev:" + rev + " tag:" + tagurl
# 				#log += svn log -r HEAD:$rev
# 		return log

		pret = subprocess.Popen("rez-svn-changelog",
							    stdout=subprocess.PIPE,
							    stderr=subprocess.PIPE)
		changelog, changelog_err = pret.communicate()
		return changelog

register_release_mode('svn', SvnRezReleaseMode)


##############################################################################
# Mercurial
##############################################################################

def hg(*args):
	cmd = ['hg'] + list(args)
	p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
	out, err = p.communicate()
	if p.returncode:
		raise RezReleaseError("failed to call: hg " + ' '.join(args) + '\n' + err)
	out = out.rstrip('\n')
	if not out:
		return []
	return out.split('\n')

class HgRezReleaseMode(RezReleaseMode):
	def __init__(self, path):
		super(HgRezReleaseMode, self).__init__(path)

		hgdir = os.path.join(self.path, '.hg')
		if not os.path.isdir(hgdir):
			raise RezReleaseUnsupportedMode("'" + self.path + "' is not an mercurial working copy")

		try:
			assert hg('root')[0] == self.path
		except AssertionError:
			raise RezReleaseUnsupportedMode("'" + self.path + "' is not the root of a mercurial working copy")
		except:
			raise RezReleaseUnsupportedMode("failed to call hg")

		self.patch_path = os.path.join(hgdir, 'patches')
		if not os.path.isdir(self.patch_path):
			self.patch_path = None

	def create_release_tag(self):
		'''
		On release, it is customary for a VCS to generate a tag
		'''
		if self.patch_path:
			# patch queue
			hg('tag', '-f', str(self.this_version),
			   '--message', self.commit_message, '--mq')
			# use a bookmark on the main repo since we can't change it
			hg('bookmark', '-f', str(self.this_version))
		else:
			hg('tag', '-f', str(self.this_version))

	def get_tags(self):
		tags = [line.split()[0] for line in hg('tags')]
		bookmarks = [line.split()[-2] for line in hg('bookmarks')]
		return tags + bookmarks

	def validate_repostate(self):
		def _check(modified, path):
			if modified:
				modified = [line.split()[-1] for line in modified]
				raise RezReleaseError("'" + path + "' is not in a state to release" +
									  " - please commit outstanding changes: " +
									  ', '.join(modified))

		_check(hg('status', '-m', '-a'), self.path)

		if self.patch_path:
			_check(hg('status', '-m', '-a', '--mq'), self.patch_path)

	def get_tag_meta_str(self):
		if self.patch_path:
			qparent = hg('log', '-r', 'qparent', '--template', '{node}')[0]
			mq_parent = hg('parent', '--mq', '--template', '{node}')[0]
			return qparent + '#' + mq_parent
		else:
			return hg('parent', '--template' '{node}')[0]

	def copy_source(self, build_dir):
		hg('archive', build_dir)

	def get_changelog(self):
		start_rev = str(self.last_tagged_version) if self.last_tagged_version else '0'
		end_rev = 'qparent' if self.patch_path else 'tip'
		log = hg('log', '-r',
				'%s..%s and not merge()' % (start_rev, end_rev),
				'--template="{desc}\n\n"')
		return ''.join(log)


register_release_mode('hg', HgRezReleaseMode)

#    Copyright 2008-2012 Dr D Studios Pty Limited (ACN 127 184 954) (Dr. D Studios)
#
#    This file is part of Rez.
#
#    Rez is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Lesser General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    Rez is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU Lesser General Public License
#    along with Rez.  If not, see <http://www.gnu.org/licenses/>.

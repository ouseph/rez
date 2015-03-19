"""
Git version control
"""
import commands
from rez.release_vcs import ReleaseVCS
from rez.utils.logging_ import print_error, print_warning, print_debug
from rez.exceptions import ReleaseVCSError
from rez.config import config
import functools
import subprocess
import re


def execute_command_in_path(command, path):
    if config.debug("package_release"):
        print_debug("Running command: %s in path %s:" % (command, path) )
    p = subprocess.Popen(command, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, cwd=path, shell=True)
    out, err = p.communicate()
    if p.returncode:
        return None
    return out.strip()


class GitReleaseVCSError(ReleaseVCSError):
    pass


class GitReleaseVCS(ReleaseVCS):

    schema_dict = {
        "allow_no_upstream": bool,
        "commit_details_format": basestring,
        "remote_origin_url": basestring}

    @classmethod
    def name(cls):
        return 'git'

    def __init__(self, path):
        super(GitReleaseVCS, self).__init__(path)
        self.executable = self.find_executable('git')

        try:
            self.git("rev-parse")
        except ReleaseVCSError:
            raise GitReleaseVCSError("%s is not a git repository" % path)

    @classmethod
    def is_valid_root(cls, path):

        inside_working_dir = execute_command_in_path('git rev-parse  --is-inside-work-tree', path)
        if not inside_working_dir == 'true':
            return False

        remote_origin_url = config.plugins.release_vcs.get(cls.name()).remote_origin_url
        current_remote_url = execute_command_in_path('git config remote.origin.url', path)

        if not current_remote_url:
            return False

        if not re.search(remote_origin_url, current_remote_url):
            return False

        return True



    def git(self, *nargs):
        return self._cmd(self.executable, *nargs)

    def get_relative_to_remote(self):
        """Return the number of commits we are relative to the remote. Negative
        is behind, positive in front, zero means we are matched to remote.  if
        we are both behind and ahead then only the ahead value will be reported.
        """
        s = self.git("status", "--short", "-b")[0]
        r = re.compile("\[([^\]]+)\]")
        toks = r.findall(s)
        if toks:
            try:
                s2 = toks[-1]
                adj, n = s2.split(",")[0].split()
                assert(adj in ("ahead", "behind"))
                n = int(n)
                return -n if adj == "behind" else n
            except Exception as e:
                raise ReleaseVCSError(
                    ("Problem parsing first line of result of 'git status "
                     "--short -b' (%s):\n%s") % (s, str(e)))
        else:
            return 0

    def get_local_branch(self):
        """Returns the label of the current local branch."""
        return self.git("rev-parse", "--abbrev-ref", "HEAD")[0]

    def get_tracking_branch(self):
        """Returns (remote, branch) tuple, or (None, None) if there is no
        remote.
        """
        try:
            remote_uri = self.git("rev-parse", "--abbrev-ref",
                                  "--symbolic-full-name", "@{u}")[0]
            return remote_uri.split('/', 1)
        except Exception as e:
            if "No upstream branch" not in str(e):
                raise e
        return (None, None)

    def validate_repostate(self):
        b = self.git("rev-parse", "--is-bare-repository")
        if b == "true":
            raise ReleaseVCSError("Could not release: bare git repository")

        remote, remote_branch = self.get_tracking_branch()

        # check for untracked files
        output = self.git("ls-files", "--other", "--exclude-standard")
        if output:
            msg = "Could not release: there are untracked files:\n"
            msg += '\n'.join(output)
            raise ReleaseVCSError(msg)

        # check for uncommitted changes
        isDirty = self.git("status", "--porcelain")
        if isDirty:
            msg = "Could not release: there are uncommitted changes:\n"
            statmsg = self.git("status")
            msg += '\n'.join(statmsg)
            raise ReleaseVCSError(msg)

        # check for upstream branch
        if remote is None and not self.settings.allow_no_upstream:
            raise ReleaseVCSError(
                "Release cancelled: there is no upstream branch (git cannot see "
                "a remote repo - you should probably FIX THIS FIRST!). To allow "
                "the release, set the config entry "
                "'plugins.release_vcs.git.allow_no_upstream' to true.")

        # check we are releasing from a valid branch
        releasable_branches = self.type_settings.releasable_branches
        if releasable_branches:
            releasable = False
            current_branch_name = self.get_local_branch()
            for releasable_branch in releasable_branches:
                if re.search(releasable_branch, current_branch_name):
                    releasable = True
                    break

            if not releasable:
                raise ReleaseVCSError(
                    "Could not release: current branch is %s, must match "
                    "one of: %s"
                    % (current_branch_name, ', '.join(releasable_branches)))

        # check if we are behind/ahead of remote
        if remote:
            self.git("remote", "update", remote)
            n = self.get_relative_to_remote()
            if n:
                s = "ahead of" if n > 0 else "behind"
                remote_uri = '/'.join((remote, remote_branch))
                raise ReleaseVCSError(
                    "Could not release: %d commits %s %s."
                    % (abs(n), s, remote_uri))

    def get_changelog(self, previous_revision=None):
        prev_commit = None
        if previous_revision is not None:
            try:
                prev_commit = previous_revision["commit"]
            except:
                if self.package.config.debug("package_release"):
                    print_debug("couldn't determine previous commit from: %r"
                                % previous_revision)

        if prev_commit:
            # git returns logs to last common ancestor, so even if previous
            # release was from a different branch, this is ok
            commit_range = "%s..HEAD" % prev_commit
            stdout = self.git("log", commit_range)
        else:
            stdout = self.git("log")
        return '\n'.join(stdout)

    def get_current_revision(self):
        doc = dict(commit=self.git("rev-parse", "HEAD")[0])

        def _url(op):
            origin = doc["tracking_branch"].split('/')[0]
            lines = self.git("remote", "-v")
            lines = [x for x in lines if origin in x.split()]
            lines = [x for x in lines if ("(%s)" % op) in x.split()]
            try:
                return lines[0].split()[1]
            except:
                raise ReleaseVCSError("failed to parse %s url from:\n%s"
                                      % (op, '\n'.join(lines)))

        def _get(key, fn):
            try:
                value = fn()
                doc[key] = value
                return (value is not None)
            except Exception as e:
                print_error("Error retrieving %s: %s" % (key, str(e)))
                return False

        def _tracking_branch():
            remote, remote_branch = self.get_tracking_branch()
            if remote is None:
                return None
            else:
                return "%s/%s" % (remote, remote_branch)

        _get("branch", self.get_local_branch)
        if _get("tracking_branch", _tracking_branch):
            _get("fetch_url", functools.partial(_url, "fetch"))
            _get("push_url", functools.partial(_url, "push"))
        return doc

    def create_release_tag(self, tag_name, message=None):
        # check if tag already exists
        tags = self.git("tag")
        if tag_name in tags:  # already exists
            return

        # create tag
        print "Creating tag '%s'..." % tag_name
        args = ["tag", "-a", tag_name]
        args += ["-m", message or '']
        self.git(*args)

        # push tag
        remote, remote_branch = self.get_tracking_branch()
        if remote is None:
            return

        remote_uri = '/'.join((remote, remote_branch))
        print "Pushing tag '%s' to %s..." % (tag_name, remote_uri)
        self.git("push", remote, tag_name)

    def get_commit_details(self, previous_revision=None):
        commit_details = []
        previous_commit = (previous_revision or {}).get("commit")

        if previous_commit:
            hashes = self.git("log", "-n", "100", "%s.." % previous_commit, "--no-merges", "--reverse",  "--pretty=%H", ".")
            for hash_ in hashes:
                commit_detail = self.git("log", hash_, "--name-only", "--no-merges", "-1", "--pretty=%s" % self.settings.commit_details_format)
                commit_details.append("\n".join(commit_detail))

        return commit_details

    def get_automatic_release_comments(self, previous_revision=None):
        automatic_release_comments = []
        previous_commit = (previous_revision or {}).get("commit")

        if previous_commit:
            hashes = self.git("log", "-n", "100", "%s.." % previous_commit, "--no-merges", "--reverse",  "--pretty=%H", ".")
            for hash_ in hashes:
                log = self.git("log", hash_, "--no-merges", "-1", "--pretty=format:%an: %s")
                message = self._get_release_message_from_log(log[0])

                if message:
                    author = self._get_author_from_log(log[0])
                    automatic_release_comments.append([author, message])

        return automatic_release_comments

    def _get_release_message_from_log(self, log):
        """
        Extract the release message from a single commit log string.  This 
        assumes that the incoming log represents a single commit and is 
        formatted to match the regular expression which is currently:

            Firstname Lastname: commit log message <release>release message</release>
        """

        return "\n".join(re.findall("(?s)<release>(.*?)</release>", log))

    def _get_author_from_log(self, log):
        """
        Extract the author from a single commit log string.  This assumes that  
        the incoming log represents a single commit and is formatted to match
        the regular expression which is currently:

            Firstname Lastname: commit log message
        """

        return re.search('^(.*?): ', log).group(1)

    def commit(self, add=True, message="Auto Commit from Rez Git VCS plugin."):
        """
        Thin wrapper for automating a commit through git.
        """

        self.git("commit", "-a" if add else "", "-m", message)


def register_plugin():
    return GitReleaseVCS

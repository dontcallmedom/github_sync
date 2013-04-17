#!/usr/bin/env python
import sys
import os
import cgi
import subprocess
import shutil
import json
import ConfigParser
import getpass
import hmac

import requests

config_path = "~/sync.ini"

class MasterCheckout(object):
    def __init__(self, path):
        self.path = path

    @classmethod
    def create(cls, path, remote):
        rv = cls(path)
        git("clone", remote, os.path.join(path, "tmp"), cwd=path)
        os.rename(os.path.join(path, "tmp", ".git"), os.path.join(path, ".git"))
        git("reset", "--hard", "HEAD", cwd=path)
        git("config", "--add", "remote.origin.fetch", "+refs/pull/*/head:refs/remotes/origin/pr/*", cwd=path)
        git("fetch", "origin", cwd=path)
        git("submodule", "init", cwd=path)
        git("submodule", "update", "--recursive", cwd=path)
        return rv

    def update(self):
        git("fetch", "origin", cwd=self.path)
        git("checkout", "-f", "origin/master", cwd=self.path)
        git("submodule", "update", "--recursive", cwd=self.path)

class PullRequestCheckout(object):
    def __init__(self, path, number):
        self.number = number
        self.path = path

    @classmethod
    def exists(cls, base_path, number):
        return os.path.exists(os.path.join(base_path, "submissions", str(number), ".git"))

    @classmethod
    def fromNumber(cls, base_path, number):
        path = os.path.join(base_path, "submissions", str(number))
        if os.path.exists(path):
            return cls(path, number)

    @classmethod
    def create(cls, base_path, number):
        path = os.path.join(base_path, "submissions", str(number))
        rv = cls(path, number)
        if not os.path.exists(path):
            os.mkdir(path)
            git("clone", "--no-checkout", base_path, path, cwd=path)
            git("submodule", "init", cwd=path)
            git("config", "--add", "remote.origin.fetch", "+refs/remotes/origin/pr/*:refs/pr/*", cwd=path)
        elif not PullRequestCheckout.exists(base_path, number):
            raise IOError("Expected git repository in path %s, got something else" % path)
        rv.update()
        return rv

    def delete(self):
        shutil.rmtree(self.path)

    def update(self):
        git("fetch", "origin", cwd=self.path)
        git("checkout", "-f", "refs/pr/%i" % self.number, "--", cwd=self.path)
        git("submodule", "update", "--recursive", cwd=self.path)

def git(command, *args, **kwargs):
    cwd = kwargs.get("cwd")
    if cwd is None:
        raise ValueError()
    no_throw = kwargs.get("no_throw", False)
    cmd = ["git", command] + list(args)
    print >> sys.stderr, cwd, repr(cmd)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd)
    stdout, stderr = proc.communicate()
    print >> sys.stderr, stdout + "\n" + stderr
    if proc.returncode != 0:
        if no_throw:
            return False
        else:
            raise IOError(stderr)
    return True

def get_authorised_users(config):
    resp = requests.get("https://api.github.com/repos/%s/%s/collaborators" % (config["org_name"], config["repo_name"]),
                        auth=(config["username"], config["password"]))
    return set(item["login"] for item in resp.json())

def process_pull_request(config, data, user_is_authorised):
    base_path = config["base_path"]

    update_master(base_path)
    action = data["action"]

    action_handlers = {"opened": pull_request_opened,
                       "reopened": pull_request_opened,
                       "closed": end_mirror,
                       "synchronize": sync_mirror}
    action_handlers[action](base_path, data["pull_request"]["number"], user_is_authorised)

def pull_request_opened(base_path, number, user_is_authorised):
    if user_is_authorised:
        start_mirror(base_path, number, user_is_authorised)

def start_mirror(base_path, number, user_is_authorised):
    if not PullRequestCheckout.exists(base_path, number):
        PullRequestCheckout.create(base_path, number)
    else:
        PullRequestCheckout.fromNumber(base_path, number).update()

def end_mirror(base_path, number, user_is_authorised):
    if PullRequestCheckout.exists(base_path, number):
        PullRequestCheckout.fromNumber(base_path, number).delete()

def sync_mirror(base_path, number, user_is_authorised):
    if PullRequestCheckout.exists(base_path, number):
        PullRequestCheckout.fromNumber(base_path, number).update()

def process_push(config):
    update_master(config["base_path"])

def command(comment):
    commands = ["mirror", "unmirror"]
    for command in commands:
        if comment.startswith("w3c-test:%s" % command):
            return command
    print >> sys.stderr, "No command found in comment"

def process_issue_comment(config, data, user_is_authorised):
    comment = data["comment"]["body"]

    if data["issue"]["pull_request"]["diff_url"] is None:
        return
    elif not user_is_authorised:
        return
    elif not command(comment):
        return
    else:
        update_master(config["base_path"])
        pull_request_number = int(data["issue"]["pull_request"]["diff_url"].rsplit("/", 1)[1])
        action_handlers = {"mirror":start_mirror,
                           "unmirror":end_mirror}
        action_handlers[command(comment)](base_path, pull_request_number, user_is_authorised)

def update_master(base_path):
    checkout = MasterCheckout(base_path)
    checkout.update()

def update_pull_requests(base_path):
    submissions_path = os.path.join(base_path, "submissions")
    for fn in os.listdir(submissions_path):
        try:
            number = int(fn)
        except ValueError:
            continue
        if PullRequestCheckout.exists(base_path, number):
            PullRequestCheckout(os.path.join(submissions_path, str(number)), number).update()

def post_authentic(config, body):
    signature = os.environ.get("HTTP_X_HUB_SIGNATURE", None)
    if not signature:
        print >> sys.stderr, "Signature missing"
        return False
    expected = "sha1=%s" % hmac.new(config["secret"], body).hexdigest()
    print >> sys.stderr, "Signature got %s, expected %s" %(signature, expected)
    return signature == expected

def main(config):
    data = sys.stdin.read()

    if data:
        print >> sys.stderr, data
#         if not post_authentic(config, data):
#             print >> sys.stderr, "Got message with incorrect signature"
#             return
        data = json.loads(data)

        authorised_users = get_authorised_users(config)

        if "commits" in data:
            process_push(config)
        else:            
            if "pull_request" in data:
                user_is_authorised = data["pull_request"]["user"]["login"] in authorised_users
                process_pull_request(config, data, user_is_authorised)
            elif "comment" in data:
                user_is_authorised = data["comment"]["user"]["login"] in authorised_users
                process_issue_comment(config, data, user_is_authorised)
            else:
                print >> sys.stderr, "Unrecognised event type with keys %r" % (data.keys(),)
    else:
        #This is a test, presumably, just update master
        update_master(config["base_path"])
        update_pull_requests(config["base_path"])

    print "Content-Type: text/plain"
    print "\r"
    print "Success"

def create_master(config):
    base_path = config["base_path"]
    if not os.path.exists(os.path.join(base_path, "submissions")):
        os.mkdir(os.path.join(base_path, "submissions"))
    if not os.path.exists(os.path.join(base_path, ".git")):
        MasterCheckout.create(base_path, "git://github.com/%s/%s.git" % (config["org_name"], config["repo_name"]))

def get_open_pull_request_numbers(config):
    authorised_users = get_authorised_users(config)
    pull_requests = requests.get("https://api.github.com/repos/%s/%s/pulls" % (config["org_name"], config["repo_name"]),
                                 auth=(config["username"], config["password"])).json()
    return [item["number"] for item in pull_requests if item["state"] == "open" and item["user"]["login"] in authorised_users]

def setup(config):
    create_master(config)
    for number in get_open_pull_request_numbers(config):
        pull_request = PullRequestCheckout.create(config["base_path"], number)
    register_events(config)

def register_events(config):
    return
    events = ["push", "pull_request", "issue_comment"]
    data = {"name":"web",
            "events":events,
            "config":{"url":config["url"],
                      "content_type":"json",
                      "secret":config["secret"]},
            "active":True
            }
    resp = requests.post("https://api.github.com/repos/%s/%s/hooks" % (config["org_name"], config["repo_name"]),
                         data=json.dumps(data), auth=(config["username"], config["password"]))
    print >> sys.stderr, "%i\n%s" % (resp.status_code, resp.text)

def get_config():
    config = ConfigParser.SafeConfigParser()
    config.read(os.path.abspath(os.path.expanduser(config_path)))
    rv = dict(config.items("sync"))
    if not "base_path" in rv:
        rv["base_path"] = os.path.abspath(os.path.split(__file__)[0])
    rv["base_path"] = os.path.abspath(os.path.expanduser(rv["base_path"]))
    return rv

if __name__ == "__main__":
    config = get_config()
    if "--setup" in sys.argv:
        setup(config)
    else:
        main(config)

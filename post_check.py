#!/usr/bin/env python2
""" New post checker """

import sys
import re
import sqlite3
import unicodedata
import json
from datetime import datetime
from time import sleep

from log_conf import LoggerManager
from common import SubRedditMod


# configure logging
LOGGER = LoggerManager().getLogger("post_check")


class PostChecker(object):
    """ Post check helper """

    def __init__(self, config, db_con, post_categories, locations):
        self._config = config
        self._user_db_con = db_con
        self._user_db_cursor = self._user_db_con.cursor()
        self._post_categories = post_categories
        self._locations = locations

    def _get_user_db_entry(self, post):
        self._user_db_cursor.execute('SELECT username, last_id, last_created as "last_created [timestamp]" '
                                     'FROM user WHERE username=?', (post.author.name,))

        return self._user_db_cursor.fetchone()

    def _update_user_db(self, post):
        post_created = datetime.utcfromtimestamp(post.created_utc)
        self._user_db_cursor.execute('UPDATE OR IGNORE user SET last_created=?, last_id=? WHERE username=?',
                                     (post_created, post.id, post.author.name))

    def _add_to_user_db(self, post):
        post_created = datetime.utcfromtimestamp(post.created_utc)
        self._user_db_cursor.execute('INSERT OR IGNORE INTO user (username, last_created, last_id) VALUES (?, ?, ?)',
                                     (post.author.name, post_created, post.id))

    def _is_personal_post(self, title):
        return bool(re.search(self._config["trade_post_format"], title))

    def _is_nonpersonal_post(self, title):
        return bool(re.search(self._config["informational_post_format"], title))

    def check_and_flair_personal(self, post, clean_title):
        """ Check title of personal post and flair accordingly """

        location, have, want = re.search(self._config["trade_post_format"], clean_title).groups()

        if "-" in location:
            primary, secondary = location.split("-")
        else:
            primary = "OTHER"
            secondary = location

        if primary not in self._locations:
            print(primary, " not in ", self._locations.keys())
            return False

        if secondary not in self._locations[primary]:
            print(secondary, " not in ", primary)
            return False

        timestamp_check = False
        post_category = self._config["default_category"]
        for category, category_prop in self._post_categories["personal"].items():
            assert not ("have" in category_prop and "want" in category_prop), "Limitation of script"
            if "want" in category_prop:
                regex = category_prop["want"].replace("\\\\", "\\")
                if re.search(regex, want, re.IGNORECASE):
                    post_category = category
                    timestamp_check = category_prop["timestamp_check"]
            if "have" in category_prop:
                regex = category_prop["have"].replace("\\\\", "\\")
                if re.search(regex, have, re.IGNORECASE):
                    post_category = category
                    timestamp_check = category_prop["timestamp_check"]
        print(clean_title, " categoryed as ", post_category)

        self.check_repost(post)

        if timestamp_check:
            print("Checking for timestamps")
            if not re.search(self._config["timestamp_regex"], post.selftext, re.IGNORECASE):
                post.report("Could not find timestamp...")

        return True

    def check_and_flair_nonpersonal(self, post, clean_title):
        """ Check title of personal post and flair accordingly """

        tag = re.search(self._config["informational_post_format"], clean_title).group(1)

        for category, category_prop in self._post_categories["nonpersonal"].items():
            if tag == category_prop["tag"]:
                print(tag, " matches ", category)
                if "required_flair" in category_prop:
                    if category_prop["required_flair"] != post.author_category_css_class:
                        print("User not having the expected category ", category_prop["required_flair"])
                        return False
                return True
        print("Bad tag ", tag)
        return False

    def check_post(self, post):
        """
        Check post for rule violations
        """

        clean_title = unicodedata.normalize('NFKD', post.title).encode('ascii', 'ignore')

        print("#"*20 + clean_title)

        if self._is_personal_post(clean_title):
            if "trade_post_format_strict" in self._config:
                if not bool(re.match(self._config["trade_post_format_strict"], clean_title)):
                    print("!"*80)
                    print(clean_title, " failed strict check")
                    return

            if not self.check_and_flair_personal(post, clean_title):
                print("!"*80)
                return

        elif self._is_nonpersonal_post(clean_title):
            # TODO: Add strict format check (not necessary at the moment)

            if not self.check_and_flair_nonpersonal(post, clean_title):
                print("!"*80)
                return

        else:
            print(clean_title, " did not match any format")
            print("!"*80)
            return

        print("Post looks fine! Commenting")

    def check_repost(self, post):
        """
        Check post for repost rule violations
        """

        db_row = self._get_user_db_entry(post)
        if db_row is not None:
            last_id = db_row["last_id"]
            last_created = db_row["last_created"]
            if post.id != last_id:
                LOGGER.info("Checking post {} for repost violation".format(post.id))
                post_created = datetime.utcfromtimestamp(post.created_utc)
                seconds_between_posts = (post_created - last_created).total_seconds()
                if (seconds_between_posts < int(self._config["upper_hour"]) * 3600 and
                        seconds_between_posts > int(self._config["lower_min"]) * 60):
                    LOGGER.info("Reported because seconds between posts: {}".format(seconds_between_posts))
                    post.report("Possible repost: https://redd.it/{}".format(last_id))
                    return
            self._update_user_db(post)
        else:
            self._add_to_user_db(post)

        self._user_db_con.commit()


def main():
    """ Main function, setups stuff and checks posts"""

    try:
        # Setup SubRedditMod
        subreddit = SubRedditMod(LOGGER)
        with open("submission_categories.json") as category_file:
            post_categories = json.load(category_file)
        with open("locations.json") as locations_file:
            locations = json.load(locations_file)

        # Setup PostChecker
        user_db = subreddit.config["trade"]["user_db"]
        db_con = sqlite3.connect(user_db, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
        db_con.row_factory = sqlite3.Row
        post_checker = PostChecker(subreddit.config["post_check"], db_con, post_categories, locations)
    except Exception as exception:
        LOGGER.error(exception)
        sys.exit()

    while True:
        try:
            first_pass = True
            processed = []
            while True:
                new_posts = subreddit.get_new(20)
                for post in new_posts:
                    if first_pass and subreddit.check_mod_reply(post):
                        processed.append(post.id)
                    if post.id in processed:
                        continue
                    post_checker.check_post(post)
                    processed.append(post.id)
                first_pass = False
                LOGGER.debug('Sleeping for 1 minute')
                sleep(60)
        except Exception as exception:
            LOGGER.error(exception)
            sleep(60)


if __name__ == '__main__':
    main()

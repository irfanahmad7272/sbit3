#/usr/bin/env python

import json
import datetime
import base64
import hmac, hashlib
import uuid

import tornado.ioloop
import tornado.web

import settings
from simpledb import SimpleDBConnection
from s3 import S3Connection


class MainHandler(tornado.web.RequestHandler):
    def get(self):
        rs = sdb_conn.domain.select("SELECT count(*) FROM `%s`" % settings.sdb_domain)
        count = [item for item in rs][0]['Count']
        self.render("index.html", count=count)


class PostHandler(tornado.web.RequestHandler):
    def _generate_policy_doc(self, conditions, expiration=None):
        if not expiration:
            # Sets a policy of 15 minutes to upload file
            expiration = datetime.datetime.utcnow() + datetime.timedelta(minutes=15)
        conditions = [ { "bucket" : conditions["bucket"] },
                       [ "starts-with", "$key", "uploads/"],
                       { "acl" : conditions["acl"] },
                       { "success_action_redirect" : conditions["success_action_redirect"] } ]
        conditions_json = json.dumps({ "expiration" : expiration.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                       "conditions" : conditions })
        return base64.b64encode(conditions_json)

    def _sign_policy(self, policy):
        signature = base64.b64encode(hmac.new(settings.aws_secret_key, policy, hashlib.sha1).digest())
        return signature

    def get(self, expiration):
        try:
            expiration = int(expiration)
            # Set max expiration to 7200 minutes (5 days)
            if not 0 < expiration < 7200:
                raise tornado.web.HTTPError(403)
            _expireTimestamp = datetime.datetime.utcnow() + datetime.timedelta(minutes=expiration)
        except ValueError:
            raise tornado.web.HTTPError(403)

        # Associate _uuid to expiration in sdb

        _uuid = uuid.uuid4().hex
        sdb_conn.add_item(_uuid, expireTimestamp=_expireTimestamp)

        conditions = { "bucket" : settings.bucket,
                       "acl" : settings.acl,
                       "success_action_redirect" : settings.site_url + "/f/" + _uuid }
        policy_document = self._generate_policy_doc(conditions)
        signature = self._sign_policy(policy_document)

        self.render("post.html", conditions=conditions,
                                 aws_access_id=settings.aws_access_id,
                                 policy_document=policy_document,
                                 signature=signature)


class GenerateUrlHandler(tornado.web.RequestHandler):
    def __init__(self, application, request, **kwargs):
        super(GenerateUrlHandler, self).__init__(application, request, **kwargs)

    def get(self, uuid):
        # Check uuid
        if uuid.isalnum() and len(uuid) == 32:
            item = sdb_conn.get_uuid(uuid)
        else:
            raise tornado.web.HTTPError(403)

        _bucket = self.get_argument('bucket')
        _key = self.get_argument('key')
        _etag = self.get_argument('etag')

        _short_url = sdb_conn.add_file(item, _bucket, _key, _etag)
        self.write(settings.site_url + '/d/' + _short_url)


class DownloadHandler(tornado.web.RequestHandler):
    def __init__(self, application, request, **kwargs):
        super(DownloadHandler, self).__init__(application, request, **kwargs)

    def get(self, shortUrl):
        try:
            sdb_item = sdb_conn.get_file(shortUrl)
        except:
            raise tornado.web.HTTPError(404)
        if sdb_item:
            s3_key_name = sdb_item[1]['key']
            s3_expiration = sdb_item[1]['expireTimestamp']
            if datetime.datetime.utcnow() < datetime.datetime.strptime(s3_expiration, "%Y-%m-%d %H:%M:%S.%f"):
                # increment download count
                sdb_conn.increment_counter(sdb_item)
                self.redirect(s3_conn.get_url(s3_key_name))
            else:
                raise tornado.web.HTTPError(403)
        else:
            raise tornado.web.HTTPError(404)


class CronHandler(tornado.web.RequestHandler):
    def get(self):
        # Get all s3 items
        all_keys = s3_conn.bucket.get_all_keys()
        for key in all_keys:
            key_record = sdb_conn.get_key(key)
            if not (key_record and datetime.datetime.now() < key_record[1].expireTimestamp):
                #key.delete()
                pass


application = tornado.web.Application([
    (r"/", MainHandler),
    (r"/u/(\d+)", PostHandler),
    (r"/f/(.*)", GenerateUrlHandler),
    (r"/d/(.*)", DownloadHandler),
    (r"/cron$", CronHandler),
])


if __name__ == "__main__":
    sdb_conn = SimpleDBConnection()
    s3_conn = S3Connection()
    application.listen(settings.site_port)
    tornado.ioloop.IOLoop.instance().start()

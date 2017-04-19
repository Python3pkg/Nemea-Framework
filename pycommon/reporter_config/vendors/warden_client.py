#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# Copyright (C) 2011-2015 Cesnet z.s.p.o
# Use of this source is governed by a 3-clause BSD-style license, see LICENSE file.

import json, httplib, ssl, socket, logging, logging.handlers, time
from urlparse import urlparse
from urllib import urlencode
from sys import stderr, exc_info
from traceback import format_tb
from os import path
from operator import itemgetter


VERSION = "3.0-beta2"

class HTTPSConnection(httplib.HTTPSConnection):
    '''
    Overridden to allow peer certificate validation, configuration
    of SSL/ TLS version and cipher selection.  See:
    http://hg.python.org/cpython/file/c1c45755397b/Lib/httplib.py#l1144
    and `ssl.wrap_socket()`
    '''
    def __init__(self, host, **kwargs):
        self.ciphers = kwargs.pop('ciphers',None)
        self.ca_certs = kwargs.pop('ca_certs',None)
        self.ssl_version = kwargs.pop('ssl_version',ssl.PROTOCOL_SSLv23)

        httplib.HTTPSConnection.__init__(self,host,**kwargs)

    def connect(self):
        sock = socket.create_connection( (self.host, self.port), self.timeout)

        if self._tunnel_host:
            self.sock = sock
            self._tunnel()

        self.sock = ssl.wrap_socket(
            sock,
            keyfile = self.key_file,
            certfile = self.cert_file,
            ca_certs = self.ca_certs,
            cert_reqs = ssl.CERT_REQUIRED if self.ca_certs else ssl.CERT_NONE,
            ssl_version = self.ssl_version)



class Error(Exception):
    """ Object for returning error messages to calling application.
        Caller can test whether it received data or error by checking
        isinstance(res, Error).
        However if he does not want to deal with errors altogether,
        this error object also returns False value if used in Bool
        context (e.g. in "if res: print res" print is not evaluated),
        and also acts as empty iterator (e.g. in "for e in res: print e"
        print is also not evaluated).
        Also, it can be raised as an exception.
    """

    def __init__(self, method=None, req_id=None, errors=None, **kwargs):

        self.errors = []
        if errors:
            self.extend(method, req_id, errors)
        if kwargs:
            self.append(method, req_id, **kwargs)


    def append(self, method=None, req_id=None, **kwargs):
        # We shift method and req_id into each and every error, because
        # we want to be able to simply merge more Error arrays (for
        # returning errors from more Warden calls at once
        if method and not "method" in kwargs:
            kwargs["method"] = method
        if req_id and not "req_id" in kwargs:
            kwargs["req_id"] = req_id
        # Ugly, but be paranoid, don't rely on server reply to be well formed
        try:
            kwargs["error"] = int(kwargs["error"])
        except Exception:
            kwargs["error"] = 0
        if "events" in kwargs:
            evlist = kwargs["events"]
            try:
                evlist_new = []
                for ev in evlist:
                    try:
                        evlist_new.append(int(ev))
                    except Exception:
                        pass
                kwargs["events"] = evlist_new
            except Exception:
                kwargs["events"] = []
        if "events_id" in kwargs:
            try:
                dummy = iter(kwargs["events_id"])
            except TypeError:
                kwargs["events_id"] = [None]*len(kwargs["events"])
        if "send_events_limit" in kwargs:
            try:
                kwargs["send_events_limit"] = int(kwargs["send_events_limit"])
            except Exception:
                del kwargs["send_events_limit"]
        self.errors.append(kwargs)


    def extend(self, method=None, req_id=None, iterable=[]):
        try:
            dummy = iter(iterable)
        except TypeError:
            iterable = []       # Bad joke from server
        for e in iterable:
            try:
                args = dict(e)
            except TypeError:
                args = {}       # Not funny!
            self.append(method, req_id, **args)


    def __len__ (self):
        """ In list or iterable context we're empty """
        return 0


    def __iter__(self):
        """ We are the iterator """
        return self


    def next(self):
        """ In list or iterable context we're empty """
        raise StopIteration


    def __bool__(self):
        """ In boolean context we're never True """
        return False


    def __str__(self):
        out = []
        for e in self.errors:
            out.append(self.str_err(e))
            out.append(self.str_info(e))
        return "\n".join(out)


    def log(self, logger=None, prio=logging.ERROR):
        if not logger:
            logger = logging.getLogger()
        for e in self.errors:
            logger.log(prio, self.str_err(e))
            info = self.str_info(e)
            if info:
                logger.info(info)
            debug = self.str_debug(e)
            if debug:
                logger.debug(debug)


    def str_preamble(self, e):
        return "%08x/%s" % (e.get("req_id", 0), e.get("method", "?"))


    def str_err(self, e):
        out = []
        out.append(self.str_preamble(e))
        out.append(" Error(%s) %s " % (e.get("error", 0), e.get("message", "Unknown error")))
        if "exc" in e and e["exc"]:
            out.append("(cause was %s: %s)" % (e["exc"][0].__name__, str(e["exc"][1])))
        return "".join(out)


    def str_info(self, e):
        ecopy = dict(e)    # shallow copy
        ecopy.pop("req_id", None)
        ecopy.pop("method", None)
        ecopy.pop("error", None)
        ecopy.pop("message", None)
        ecopy.pop("exc", None)
        if ecopy:
            out = "%s Detail: %s" % (self.str_preamble(e), json.dumps(ecopy, default=lambda v: str(v)))
        else:
            out = ""
        return out


    def str_debug(self, e):
        out = []
        out.append(self.str_preamble(e))
        if not "exc" in e or not e["exc"]:
            return ""
        exc_tb = e["exc"][2]
        if exc_tb:
            out.append("Traceback:\n")
            out.extend(format_tb(exc_tb))
        return "".join(out)



class Client(object):

    def __init__(self,
            url,
            certfile=None,
            keyfile=None,
            cafile=None,
            timeout=60,
            retry=3,
            pause=5,
            get_events_limit=6000,
            send_events_limit=500,
            errlog={},
            syslog=None,
            filelog=None,
            idstore=None,
            name="org.example.warden.test",
            secret=None):

        self.name = name
        self.secret = secret
        # Init logging as soon as possible and make sure we don't
        # spit out exceptions but just log or return Error objects
        self.init_log(errlog, syslog, filelog)

        self.url = urlparse(url, allow_fragments=False)

        self.conn = None

        base = path.join(path.dirname(__file__))
        self.certfile = path.join(base, certfile or "cert.pem")
        self.keyfile  = path.join(base, keyfile or "key.pem")
        self.cafile = path.join(base, cafile or "ca.pem")
        self.timeout = int(timeout)
        self.get_events_limit = int(get_events_limit)
        self.idstore = path.join(base, idstore) if idstore is not None else None

        self.send_events_limit = int(send_events_limit)
        self.retry = int(retry)
        self.pause = int(pause)

        self.ciphers = 'TLS_RSA_WITH_AES_256_CBC_SHA'
        self.sslversion = ssl.PROTOCOL_TLSv1

        self.getInfo()  # Call to align limits with server opinion


    def init_log(self, errlog, syslog, filelog):

        def loglevel(lev):
            try:
                return int(getattr(logging, lev.upper()))
            except (AttributeError, ValueError):
                self.logger.warning("Unknown loglevel \"%s\", using \"debug\"" % lev)
                return logging.DEBUG

        def facility(fac):
            try:
                return int(getattr(logging.handlers.SysLogHandler, "LOG_" + fac.upper()))
            except (AttributeError, ValueError):
                self.logger.warning("Unknown syslog facility \"%s\", using \"local7\"" % fac)
                return logging.handlers.SysLogHandler.LOG_LOCAL7

        form = "%(filename)s[%(process)d]: %(name)s (%(levelname)s) %(message)s"
        format_notime = logging.Formatter(form)
        format_time = logging.Formatter('%(asctime)s ' + form)

        self.logger = logging.getLogger(self.name)
        self.logger.propagate = False   # Don't bubble up to root logger
        self.logger.setLevel(logging.DEBUG)

        if errlog is not None:
            el = logging.StreamHandler(stderr)
            el.setFormatter(format_time)
            el.setLevel(loglevel(errlog.get("level", "info")))
            self.logger.addHandler(el)

        if filelog is not None:
            try:
                fl = logging.FileHandler(
                    filename=path.join(
                        path.dirname(__file__),
                        filelog.get("file", "%s.log" % self.name)),
                        encoding="utf-8")
                fl.setLevel(loglevel(filelog.get("level", "debug")))
                fl.setFormatter(format_time)
                self.logger.addHandler(fl)
            except Exception as e:
                Error(message="Unable to setup file logging", exc=exc_info()).log(self.logger)

        if syslog is not None:
            try:
                sl = logging.handlers.SysLogHandler(
                    address=syslog.get("socket", "/dev/log"),
                    facility=facility(syslog.get("facility", "local7")))
                sl.setLevel(loglevel(syslog.get("level", "debug")))
                sl.setFormatter(format_notime)
                self.logger.addHandler(sl)
            except Exception as e:
                Error(message="Unable to setup syslog logging", exc=exc_info()).log(self.logger)

        if not (errlog or filelog or syslog):
            # User wants explicitly no logging, so let him shoot his socks off.
            # This silences complaining of logging module about no suitable
            # handler.
            self.logger.addHandler(logging.NullHandler())


    def log_err(self, err, prio=logging.ERROR):
        if isinstance(err, Error):
            err.log(self.logger, prio)
        return err


    def connect(self):

        try:
            if self.url.scheme=="https":
                conn = HTTPSConnection(
                    self.url.netloc,
                    strict = False,
                    key_file = self.keyfile,
                    cert_file = self.certfile,
                    timeout = self.timeout,
                    ciphers = self.ciphers,
                    ca_certs = self.cafile,
                    ssl_version = self.sslversion)
            elif self.url.scheme=="http":
                conn = httplib.HTTPConnection(
                    self.url.netloc,
                    strict = False,
                    timeout = self.timeout)
            else:
                return Error(message="Don't know how to connect to \"%s\"" % self.url.scheme,
                        url=self.url.geturl())
        except Exception:
            return Error(message="HTTP(S) connection failed", exc=exc_info(),
                    url=self.url.geturl(),
                    timeout=self.timeout,
                    key_file=self.keyfile,
                    cert_file=self.certfile,
                    cafile=self.cafile,
                    ciphers=self.ciphers,
                    ssl_version=self.sslversion)

        return conn


    def sendRequest(self, func="", payload=None, **kwargs):

        if self.secret is None:
            kwargs["client"] = self.name
        else:
            kwargs["secret"] = self.secret

        if kwargs:
            for k in kwargs.keys():
                if kwargs[k] is None:
                    del kwargs[k]
            argurl = "?" + urlencode(kwargs, doseq=True)
        else:
            argurl = ""

        try:
            if payload is None:
                data = ""
            else:
                data = json.dumps(payload)
        except:
            return Error(message="Serialization to JSON failed",
                exc=exc_info(), method=func, payload=payload)

        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Content-Length": str(len(data))
        }

        # HTTP(S)Connection is oneshot object (and we don't speak "pipelining")
        conn = self.connect()
        if not conn:
            return conn  # either False of Error instance

        loc = '%s/%s%s' % (self.url.path, func, argurl)
        try:
            conn.request("POST", loc, data, self.headers)
        except:
            conn.close()
            return Error(message="Sending of request to server failed",
                exc=exc_info(), method=func, log=loc, headers=self.headers, data=data)

        try:
            res = conn.getresponse()
        except:
            conn.close()
            return Error(method=func, message="HTTP reply failed",
                exc=exc_info(), loc=loc, headers=self.headers, data=data)

        try:
            response_data = res.read()
        except:
            conn.close()
            return Error(method=func, message="Fetching HTTP data from server failed",
                exc=exc_info(), loc=loc, headers=self.headers, data=data)

        conn.close()

        if res.status==httplib.OK:
            try:
                data = json.loads(response_data)
            except:
                data = Error(method=func, message="JSON message parsing failed",
                    exc=exc_info(), response=response_data)
        else:
            try:
                data = json.loads(response_data)
                data["errors"]   # trigger exception if not dict or no error key
            except:
                data = Error(method=func, message="Generic server HTTP error",
                    error=res.status, exc=exc_info(), response=response_data)
            else:
                data = Error(
                    method=data.get("method", None),
                    req_id=data.get("req_id", None),
                    errors=data.get("errors", []))

        return data


    def _saveID(self, id, idstore=None):
        idf = idstore or self.idstore
        if not idf:
            return False
        try:
            with open(idf, "w+") as f:
                f.write(str(id))
        except (ValueError, IOError) as e:
            # Use Error instance just for proper logging
            Error(message="Writing id file \"%s\" failed" % idf, exc=exc_info(),
                  idstore=idf).log(self.logger, logging.INFO)
        return id


    def _loadID(self, idstore=None):
        idf = idstore or self.idstore
        if not idf:
            return None
        try:
            with open(idf, "r") as f:
                id = int(f.read())
        except (ValueError, IOError) as e:
            Error(message="Reading id file \"%s\" failed, relying on server" % idf,
                  exc=exc_info(), idstore=idf).log(self.logger, logging.INFO)
            id = None
        return id


    def getDebug(self):
        return self.log_err(self.sendRequest("getDebug"))


    def getInfo(self):
        res = self.sendRequest("getInfo")
        if isinstance(res, Error):
            res.log(self.logger)
        else:
            try:
                self.send_events_limit = min(res["send_events_limit"], self.send_events_limit)
                self.get_events_limit = min(res["get_events_limit"], self.get_events_limit)
            except (AttributeError, TypeError, KeyError):
                pass
        return res


    def send_events_raw(self, events=[]):
        return self.sendRequest("sendEvents", payload=events)


    def send_events_chunked(self, events=[]):
        """ Split potentially long "events" list to send_events_limit
            long chunks to avoid slap from server.
        """
        count = len(events)
        err = Error()
        send_events_limit = self.send_events_limit  # object stored value can change during sending
        for offset in range(0, count, send_events_limit):
            res = self.send_events_raw(events[offset:min(offset+send_events_limit, count)])

            if isinstance(res, Error):
                # Shift all error indices by offset to correspond with 'events' list
                for e in res.errors:
                    evlist = e.get("events", [])
                    # Update sending limit advice, if present in error
                    srv_limit = e.get("send_events_limit")
                    if srv_limit:
                        self.send_events_limit = min(self.send_events_limit, srv_limit)
                    for i in range(len(evlist)):
                        evlist[i] += offset
                err.errors.extend(res.errors)

        return err if err.errors else {}


    def sendEvents(self, events=[], retry=None, pause=None):
        """ Send out "events" list to server, retrying on server errors.
        """
        ev = events
        idx_xlat = range(len(ev))
        err = Error()
        retry = retry or self.retry
        attempt = retry
        while ev and attempt:
            if attempt<retry:
                self.logger.info("%d transient errors, retrying (%d to go)" % (len(ev), attempt))
                time.sleep(pause or self.pause)
            res = self.send_events_chunked(ev)
            attempt -= 1

            next_ev = []
            next_idx_xlat = []
            if isinstance(res, Error):
                # Sort to process fatal errors first
                res.errors.sort(key=itemgetter("error"))
                for e in res.errors:
                    errno = e["error"]
                    evlist = e.get("events", range(len(ev)))   # none means all
                    if errno < 500 or not attempt:
                        # Fatal error or last try, translate indices
                        # to original and prepare for returning to caller
                        for i in range(len(evlist)):
                            evlist[i] = idx_xlat[evlist[i]]
                        err.errors.append(e)
                    else:
                        # Maybe transient error, prepare to try again
                        for evlist_i in evlist:
                            next_ev.append(ev[evlist_i])
                            next_idx_xlat.append(idx_xlat[evlist_i])
            ev = next_ev
            idx_xlat = next_idx_xlat

        return self.log_err(err) if err.errors else {"saved": len(events)}


    def getEvents(self, id=None, idstore=None, count=None,
            cat=None, nocat=None,
            tag=None, notag=None,
            group=None, nogroup=None):

        if not id:
            id = self._loadID(idstore)

        res = self.sendRequest(
            "getEvents", id=id, count=count or self.get_events_limit, cat=cat,
            nocat=nocat, tag=tag, notag=notag, group=group, nogroup=nogroup)

        if res:
            try:
                events = res["events"]
                newid = res["lastid"]
            except KeyError:
                events = Error(method="getEvents", message="Server returned bogus reply",
                    exc=exc_info(), response=res)
            self._saveID(newid)
        else:
            events = res

        return self.log_err(events)


    def close(self):

        if hasattr(self, "conn") and hasattr(self.conn, "close"):
            self.conn.close()


    __del__ = close



def format_timestamp(epoch=None, utc=True, utcoffset=None):
    if utcoffset is None:
        utcoffset = -(time.altzone if time.daylight else time.timezone)
    if epoch is None:
        epoch = time.time()
    if utc:
        epoch += utcoffset
    us = int(epoch % 1 * 1000000 + 0.5)
    return format_time(*time.gmtime(epoch)[:6], microsec=us, utcoffset=utcoffset)


def format_time(year, month, day, hour, minute, second, microsec=0, utcoffset=None):
    if utcoffset is None:
        utcoffset = -(time.altzone if time.daylight else time.timezone)
    tstr = "%04d-%02d-%02dT%02d:%02d:%02d" % (year, month, day, hour, minute, second)
    usstr = "." + str(microsec).rstrip("0") if microsec else ""
    offsstr = ("%+03d:%02d" % divmod((utcoffset+30)//60, 60)) if utcoffset else "Z"
    return tstr + usstr + offsstr


def read_cfg(cfgfile):
    abspath = path.join(path.dirname(__file__), cfgfile)
    with open(abspath, "r") as f:
        stripcomments = "\n".join((l for l in f if not l.lstrip().startswith(("#", "//"))))
        return json.loads(stripcomments)

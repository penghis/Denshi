# -*- coding: utf-8 -*-

import sqlite3
import logging
import time
try:
    from naoko.settings import LOG_LEVEL
except:
    # This probably only happens when executing this directly
    print "Defaulting to LOG_LEVEL debug [%s]" % (__name__)
    LOG_LEVEL = logging.DEBUG


ProgrammingError = sqlite3.ProgrammingError
DatabaseError    = sqlite3.DatabaseError


def dbopen(fn):
    """
    Decorator that ensures fn is only executed on an open database.
    """

    def dbopen_func(self, *args, **kwargs):
        if self._state == "open":
            return fn(self, *args, **kwargs)
        elif self._state == "closed":
            raise DatabaseError("Cannot perform operations on closed database")
        else:
            raise DatabaseError("Database must be open to perform operations")
    return dbopen_func


class NaokoCursor(sqlite3.Cursor):
    """
    Subclass of sqlite3.Cursor that implements the context manager protocol
    """

    _id = 0

    def __init__(self, *args, **kwargs):
        self.logger = logging.getLogger('naokocursor')
        self.id = NaokoCursor._id
        NaokoCursor._id += 1
        sqlite3.Cursor.__init__(self, *args, **kwargs)

    def __enter__(self):
        return self

    def __str__(self):
        return "NaokoCursor #%d" % self.id

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        if not self.logger:
            return
        if exc_type and exc_value:
            self.logger.error("%s closed  %s: %s" % (self, exc_type, exc_value))
        else:
            self.logger.debug("%s closed" % self)


class NaokoDB(object):
    """
    Wrapper around an sqlite3 database. Roughly analagous
    to an sqlite3.Connection.

 err   This is _NOT_ a subclass of sqlite3.Connection.

    Implements the context manager protocol.
    """

    _dbinfo_sql = "SELECT name FROM sqlite_master WHERE type='table'"
    _version_sql = "SELECT value FROM metadata WHERE key='dbversion'"
    _required_tables = set(['video_stats', 'videos', 'chat'])

    def _getTables(self):
        with self.execute(self._dbinfo_sql) as cur:
            return set([table[0] for table in cur.fetchall()])

    def _getVersion(self):
        tables = self._getTables()
        if 'metadata' in tables:
            with self.execute(self._version_sql) as cur:
                version = cur.fetchone()[0]
                self.logger.debug("Database version is %s" % version)
                return int(version)
        else: # There was no explicit version in the original database
            self.logger.debug("Database version is 1 (no metadata table)")
            return 1

    # Upgrade version 1 of the database to version 2
    # This approach duplicates information from naoko.sql
    # however upgrade procedures in general should probably be done
    # by hand
    def _upgrade_1_2(self):
        print "Upgrading Naoko's Database from version 1 to version 2"
        upgrade_stmts = [
            'CREATE TABLE metadata(key TEXT, value TEXT, PRIMARY KEY(key))',
            'CREATE TABLE chat(timestamp INTEGER, username TEXT, userid TEXT, msg TEXT, protocol TEXT, channel TEXT, flags TEXT)',
            'CREATE INDEX chat_ts   ON chat(timestamp)',
            'CREATE INDEX chat_user ON chat(username)',
            "INSERT INTO metadata(key, value) VALUES ('dbversion', '2')"]
        for stmt in upgrade_stmts:
            self.executeDML(stmt)

    def _setupSchema(self):
        tables = self._getTables()
        # run self.initscript if we have an empty db (one with no tables)
        if len(tables) is 0:
            self.initdb()
        else:
            # DATABASE_UPGRADE
            # Try and sensibly handle upgrades from a lower version of the
            # database to a higher one. Currently only 1->2 is supported
            # and as this upgrade is non-destructive, we can just
            # run the init script and ignore errors. this may change in
            # the future.
            version = self._getVersion()
            if version == 1:
                self._upgrade_1_2()
            elif version == 2:
                pass # database schema is current
            else:
                self.logger.warn("UNKNOWN DATABASE VERSION: %s" % (version))
         


    # Low level database handling methods
    def __enter__(self):
        return self

    def __init__(self, database, initscript):
        self.logger = logging.getLogger("database")
        self.logger.setLevel(LOG_LEVEL)
        self.initscript = initscript
        self.db_file = database
        self.con = sqlite3.connect(database)
        self._state = "open"

        self._setupSchema()
        tables = self._getTables()

        if not self._required_tables <= tables:
            raise ValueError("Database '%s' is non-empty but "
                            "does not provide required tables %s" %
                            (database, self._required_tables - tables))


    def __exit__(self, exc_type, exc_val, exc_tb):
        self._state = "closed"
        if self.con:
            self.con.close()
        if exc_type and exc_val:
            self.logger.error("Database '%s' closed due to %s: %s" %
                            (self.db_file, exc_type, exc_val))
        else:
            self.logger.debug("Database '%s' closed" % self.db_file)

    @dbopen
    def initdb(self):
        """
        Initializes an empty sqlite3 database using .initscript.
        """
        self.logger.debug("Running initscript\n%s" % (self.initscript))
        with self.executescript(self.initscript):
            self.con.commit()

    @dbopen
    def cursor(self):
        """
        Returns an open cursor of type NaokoCursor.
        """
        return self.con.cursor(NaokoCursor)

    @dbopen
    def execute(self, stmt, *args):
        """
        Opens a cursor using .cursor and executes stmt.

        Returns the open cursor.

        This method should not be directly called unless the returned cursor is handled.
        """
        cur = self.cursor()
        cur.execute(stmt, *args)
        return cur

    @dbopen
    def executeDML(self, stmt, *args):
        """
        Executes a statement without returning an open cursor.
        """
        with self.execute(stmt, *args):
            pass

    @dbopen
    def commit(self):
        """
        Commits changes to the database.

        This method exists because python 2.7.2 introduced a bug when con.commit() is called with a select statement.
        """
        self.con.commit()

    @dbopen
    def executescript(self, script):
        """
        Opens a cursor using .cursor and executes script.

        Returns the open cursor.
        """
        cur = self.cursor()
        cur.executescript(script)
        return cur

    @dbopen
    def fetch(self, stmt, *args):
        """
        Executes stmt and fetches all the rows.

        stmt must be a select statement.

        Returns the fetched rows.
        """
        with self.execute(stmt, *args) as cur:
            return cur.fetchall()

    def close(self):
        """
        Closes the associated sqlite3 connection.
        """
        self.__exit__(None, None, None)


    # Higher level video/poll/chat-related APIs
    def getVideos(self, num=None, columns=None, orderby=None):
        """
        Retrieves videos from the video_stats table of Naoko's database.

        num must be an integer specifying the maximum number of rows to return.
        By default all rows are retrieved

        columns must be an iterable specifying which columns to retrieve. By default
        all columns will be retrieved. See naoko.sql for database schema.

        orderby must be a tuple specifying the orderby clause. Valid values are
        ('id', 'ASC'), ('id', 'DESC'), or ('RANDOM()')

        The statement executed against the database will roughly be
        SELECT <columns> FROM video_stats vs, videos v
            WHERE vs.type = v.type AND vs.id = v.id
            [ORDER BY <orderby>] [LIMIT ?]
        """


        _tables = {'videos'      : set(['type', 'id', 'duration_ms', 'title']),
                    'video_stats' : set(['type', 'id', 'uname'])}
        legal_cols = set.union(_tables['videos'], _tables['video_stats'])
        if not columns:
            columns = legal_cols

        if not set(columns) <= legal_cols:
            raise ProgrammingError("Argument columns: %s not a subset of video "
                                    "columns %s" % (columns, _tables['videos']))

        # Canonicalize references to columns
        col_repl = {'id'   : 'v.id',
                    'type' : 'v.type'}

        sel_cols = []
        for col in columns:
            sel_col = col
            if col in col_repl:
                sel_col = col_repl[col]
            sel_cols.append(sel_col)

        sel_list  = ', '.join(sel_cols)
        sel_cls   = 'SELECT %s' % (sel_list)
        from_cls  = ' FROM video_stats vs, videos v '
        where_cls = ' WHERE vs.type = v.type AND vs.id = v.id '

        sql = sel_cls + from_cls + where_cls

        def matchOrderBy(this, other):
            valid = this == other
            if not valid:
                valid = (len(this) == 2) and (len(other) == 2)
                for i in range(len(this)):
                    valid = valid and (this[i].lower() == other[1].lower())
            return valid

            valid = this and other and (this[0].lower() != other[0].lower())
            if valid and (len(this) == 2) and this[1] and other[1]:
                return valid and (this[1].lower() == other[1].lower())
            else:
                return valid and (this[1] == other[1])

        if orderby is None:
            pass
        elif matchOrderBy(orderby, ('id', 'ASC')):
            sql += ' ORDER BY v.id ASC'
        elif matchOrderBy(orderby, ('id', 'DESC')):
            sql += ' ORDER BY v.id DESC'
        elif matchOrderBy(orderby, ('RANDOM()',)):
            sql += ' ORDER BY RANDOM()'
        else:
            raise ProgrammingError("Invalid orderby %s" % (orderby))

        binds = ()
        if isinstance(num, (int, long)):
            sql += ' LIMIT ?'
            binds += (num,)
        elif  num != None:
            raise ProgrammingError("Invalid num %s" % (num))
                                 
        self.logger.debug("Generated SQL %s" % (sql))

        with self.execute(sql, binds) as cur:
            return cur.fetchall()

    def insertChat(self, msg, username, userid=None, timestamp=None, 
                   protocol='ST', channel=None, flags=None):
        """
        Insert chat message into the chat table of Naoko's database.

        msg is the chat message to be inserted.

        username is the sender of the chat message.

        userid is the userid of the sender of the chat message. This may
        not make sense for all protocols. By default it is None.

        timestamp is the timestamp the message was received. If None
        timestamp will default to the time insertChat was called.

        proto is the protocol over which the message was sent. The
        default protocol is 'ST'.

        channel is the channel or room for which the message was 
        intended. The default is None.

        flags are any miscellaneous flags attached to the user/message.
        this is intended to denote things such as emotes, video adds, etc.
        By default it is None.
        """
        if userid is None:
            userid = username
        if timestamp is None:
            timestamp = int(time.time())
            
        chat = (timestamp, username, userid, msg, protocol, channel, flags)
        with self.cursor() as cur:
            self.logger.debug("Inserting chat message %s" % (chat,))
            cur.execute("INSERT INTO chat VALUES(?, ?, ?, ?, ?, ?, ?)", chat)
        self.commit()

            
def _upgrade_tests(db):
    print "**Testing database upgrades"                                     
    assert db._getVersion() == 2, "Newly created database NOT version 2"
    
    # Simulate a dbversion 1 database by deleting new tables
    print "**Downgrading database to version 1"
    with db.cursor() as cur:
        drop_tables  = ('metadata', 'chat')
        for table in drop_tables:
            print "**Dropping %s" % (table,)
            cur.execute("DROP TABLE %s" % (table,))
            
    assert db._getVersion() == 1, "Downgraded database NOT version 1"
    db._setupSchema()
    assert db._getVersion() == 2, "Upgraded database NOT version 2"  
    print "**UPGRADE TESTS SUCCEEDED\n\n\n"
                           

# Run some tests if called directly
if __name__ == '__main__':
    logging.basicConfig(format='%(name)-15s:%(levelname)-8s - %(message)s')
    cur_log = logging.getLogger('naokocursor')
    db_log  = logging.getLogger('database')
    loggers = [cur_log, db_log]
    [logger.setLevel(LOG_LEVEL) for logger in loggers]

    vids = [('yt', 'Fp7dKcCBXHI', 37 * 1000,
                u'【English Sub】 Kyouko Canyon 【Yuru Yuri】'),
            ('yt', 'N_5Lllcj3rY', 103 * 1000,
                u'Jumping Kyouko [YuruYuri Eyecatch MAD]')]

    inserts = [('yt', 'N_5Lllcj3rY', 'Falaina'),
                ('yt', 'Fp7dKcCBXHI', 'Desuwa'),
                ('yt', 'N_5Lllcj3rY', 'Fukkireta'),
                ('yt', 'N_5Lllcj3rY', 'Fukkoreta')]

    print "**Testing database creation with :memory: database"

    with NaokoDB(':memory:', file('naoko.sql').read()) as db:
        _upgrade_tests(db)

        print "**Testing chat message insertion"
        # Test defaults
        db.insertChat('Message 2', 'falaina')

        # Trivial test
        db.insertChat('Message 1', 'Kaworu', userid=None, timestamp='1',
                      protocol='IRC', channel='Denshi', flags='o')

        with db.cursor() as cur:
            cur.execute('SELECT * FROM chat ORDER by timestamp');

            rows = cur.fetchall()
            print "**Retrieved rows: %s" % (rows,)

            assert len(rows) == 2, 'Inserted 2 rows, but did not retrieve 2'
            
            # The first message should be Kaworu: Message 1 due to 
            # a timestamp of 1
            assert rows[0][0] is 1, 'Incorrect timestamp'
            assert rows[0][1] == 'Kaworu', 'Incorrect name for Kaworu'

            # Second message should be Falaina: Message 2
            assert rows[1][3] == 'Message 2', 'Incorrect message for Falaina'
            assert rows[1][4] == 'ST', 'Incorrect protocol for Falaina'

        print "**CHAT TESTS SUCCEEDED\n\n\n"
        with db.cursor() as cur:
            print "**Inserting videos into database: %s" % (vids)
            cur.executemany("INSERT INTO videos VALUES(?, ?, ?, ?)", vids)
            cur.executemany("INSERT OR IGNORE INTO videos VALUES(?, ?, ?, ?)", vids)
            cur.execute("SELECT * FROM videos ORDER BY id")
            row = cur.fetchone()
            while row:
                print "**Retrieved row: %s" % (row,)
                row = cur.fetchone()


            print "**Inserting video stats into database: %s" % (inserts)
            cur.executemany("INSERT INTO video_stats VALUES(?, ?, ?)", inserts)

        # Lets run some quick sanity queries
        with db.cursor() as cur:
            cur.execute("SELECT * FROM video_stats")
            rows = cur.fetchall()
            print "**Retrived rows: %s" % (rows,)
            assert rows == inserts, "Retrieved does not match inserted"


        assert len(db.getVideos(1)) == 1, 'db.getVideos(1) did not return 1'

        rand_rows = db.getVideos(5, None, ('RANDOM()',))
        assert len(rand_rows)  <= 5, 'db.getVideos(5) did not return less than 5'

        all_rows  = db.getVideos(None, None)
        print all_rows

        # Keep fetching videos until we've seen all 24 different permutations
        # The chance of not seeing a permutation at least once over
        # 10000 iterations is vanishingly small
        resultsets = set()
        attempts = 10000
        print "**Attempting to retrieve all permutations using RANDOM() orderby"
        for i in range(attempts):
            # Disable logging for sanity
            [logger.setLevel(logging.WARN) for logger in loggers]
            rand_rows = tuple(db.getVideos(4, None, ('RANDOM()',)))
            resultsets.add(rand_rows)
            if len(resultsets) == 24:
                break

        if len(resultsets) != 24:
            raise AssertionError("10000 random selects did not produce all 24"
                        " permutations of the four videos"
                        " \n[Actual:   %s]\n[Expected: %s]"
                        % (len(resultsets), 24))

        print "**Found all permutations in %d iterations." % (i,)

        # Reenable logging
        [logger.setLevel(LOG_LEVEL) for logger in loggers]

        sorted_rows = db.getVideos(4, set(['id']), ('id', 'ASC'))
        if sorted_rows != sorted(sorted_rows):
            raise AssertionError("sorted rows not actually sorted\n"
                        "[Actual:   %s]\n[Expected: %s]"
                        % (sorted_rows, sorted(sorted_rows)))

        reversed_rows = db.getVideos(4, ['id'], ('id', 'DESC'))
        expected_rows = sorted(reversed_rows, reverse=True)
        if reversed_rows != expected_rows:
            raise AssertionError("reversed rows not actually reversed\n"
                        "[Actual:   %s]\n[Expected: %s]"
                        % (reversed_rows, expected_rows))
        print "**VIDEO TESTS SUCCEEDED\n\n\n"



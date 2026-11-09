// Minimal Qt harness that opens viber.db through Viber's OWN sqlite plugin (which applies
// Viber's custom key transform/codec), using the key captured by hexkey_hook.c.
//
// Built against system Qt6 headers but run with LD_LIBRARY_PATH=/opt/viber/lib and
// QT_PLUGIN_PATH=/opt/viber/plugins so the QSQLITE driver is Viber's libqsqlite.so.
//
// Our own account / our own data. Reads only. Key comes from env VIBER_HEXKEY (never argv,
// so it doesn't show up in process listings). DB path from env VIBER_DB (use a copy).
//   argv[1..] : SQL statements to run (defaults to listing tables). Rows printed tab-separated.

#include <QCoreApplication>
#include <QSqlDatabase>
#include <QSqlError>
#include <QSqlQuery>
#include <QSqlRecord>
#include <QString>
#include <QStringList>
#include <cstdio>
#include <cstdlib>

static bool run(QSqlDatabase &db, const QString &sql, bool printRows) {
    QSqlQuery q(db);
    if (!q.exec(sql)) {
        fprintf(stderr, "ERR exec: %s\n", q.lastError().text().toUtf8().constData());
        return false;
    }
    if (printRows) {
        int cols = q.record().count();
        while (q.next()) {
            QStringList row;
            for (int i = 0; i < cols; i++) row << q.value(i).toString();
            printf("%s\n", row.join("\t").toUtf8().constData());
        }
    }
    return true;
}

int main(int argc, char **argv) {
    QCoreApplication app(argc, argv);

    const char *dbPath = getenv("VIBER_DB");
    const char *hexkey = getenv("VIBER_HEXKEY");
    if (!dbPath || !hexkey) {
        fprintf(stderr, "set VIBER_DB and VIBER_HEXKEY\n");
        return 2;
    }

    {
        QSqlDatabase db = QSqlDatabase::addDatabase("QSQLITE");
        db.setDatabaseName(QString::fromUtf8(dbPath));
        if (!db.open()) {
            fprintf(stderr, "ERR open: %s\n", db.lastError().text().toUtf8().constData());
            return 3;
        }
        // key first, before any read. Viber's plugin uses `PRAGMA hexkey='<hex>'` (its own
        // extension that triggers the custom key transform) — this is the exact form Viber issues.
        if (!run(db, QString("PRAGMA hexkey='%1'").arg(hexkey), false)) return 4;
        // optional extra pragmas (cipher settings) via env, semicolon-separated
        if (const char *extra = getenv("VIBER_PRAGMAS")) {
            for (const QString &p : QString::fromUtf8(extra).split(';', Qt::SkipEmptyParts))
                run(db, p.trimmed(), false);
        }

        if (argc <= 1) {
            run(db, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name", true);
        } else {
            for (int i = 1; i < argc; i++)
                if (!run(db, QString::fromUtf8(argv[i]), true)) { db.close(); return 5; }
        }
        db.close();
    }
    QSqlDatabase::removeDatabase(QSqlDatabase::defaultConnection);
    return 0;
}

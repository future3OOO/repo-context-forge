-- SoulForge 2.13.2 native repomap.db (files/edges/refs/external_imports/symbols/calls/cochanges),
-- produced by `soulforge --headless --quiet --no-render --timeout 90000 "Reply exactly: OK"`
-- over a committed Python corpus whose ground-truth importers are:
--   pkg/lib/alpha.py    <- pkg/eta.py (import pkg.lib.alpha),
--                         pkg/gamma.py (import pkg.lib.alpha as alpha_mod),
--                         pkg/lib/__init__.py (from .alpha import helper_alpha),
--                         pkg/lib/beta.py (from .alpha import helper_alpha),
--                         pkg/sub/delta.py (from pkg.lib import alpha),
--                         tools/run-it.py (from pkg.lib.alpha import helper_alpha, other_alpha)
--   pkg/lib/__init__.py <- pkg/theta.py (from pkg.lib import helper_alpha: a name the package
--                         re-exports, not a submodule, so it binds to __init__.py)
--   pkg/lib/beta.py     <- pkg/sub/eps.py (from ..lib.beta import beta_thing)
--   pkg/zeta.py         <- nobody (it imports only json)
-- Every refs.source_file_id on an import statement is NULL: the producer records the
-- statement text (refs.import_source and external_imports.package) and resolves none of it.
CREATE TABLE calls (
        caller_symbol_id INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
        callee_name TEXT NOT NULL,
        callee_symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL,
        callee_file_id INTEGER REFERENCES files(id) ON DELETE CASCADE,
        line INTEGER NOT NULL
      );
INSERT INTO "calls" VALUES(1,'helper_alpha',3,5,4);
INSERT INTO "calls" VALUES(2,'helper_alpha',3,5,4);
INSERT INTO "calls" VALUES(6,'helper_alpha',3,5,4);
CREATE TABLE cochanges (
        file_id_a INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        file_id_b INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        count INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (file_id_a, file_id_b)
      );
CREATE TABLE edges (
        source_file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        target_file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        weight REAL NOT NULL DEFAULT 1.0, confidence INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (source_file_id, target_file_id)
      );
INSERT INTO "edges" VALUES(1,2,0.5,1);
INSERT INTO "edges" VALUES(2,1,0.5,1);
INSERT INTO "edges" VALUES(3,1,0.5,1);
INSERT INTO "edges" VALUES(4,5,0.5,1);
INSERT INTO "edges" VALUES(5,4,0.5,1);
INSERT INTO "edges" VALUES(6,4,0.5,1);
INSERT INTO "edges" VALUES(7,8,0.5,1);
INSERT INTO "edges" VALUES(8,7,0.5,1);
INSERT INTO "edges" VALUES(9,7,0.5,1);
INSERT INTO "edges" VALUES(10,1,0.5,1);
INSERT INTO "edges" VALUES(11,1,0.5,1);
CREATE TABLE external_imports (
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        package TEXT NOT NULL,
        specifiers TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (file_id, package)
      );
INSERT INTO "external_imports" VALUES(2,'import pkg.lib.alpha','alpha');
INSERT INTO "external_imports" VALUES(3,'import pkg.lib.alpha as alpha_mod','alpha');
INSERT INTO "external_imports" VALUES(4,'from .alpha import helper_alpha','helper_alpha');
INSERT INTO "external_imports" VALUES(6,'from .alpha import helper_alpha','helper_alpha');
INSERT INTO "external_imports" VALUES(8,'from pkg.lib import alpha','lib,alpha');
INSERT INTO "external_imports" VALUES(9,'from ..lib.beta import beta_thing','beta_thing');
INSERT INTO "external_imports" VALUES(10,'from pkg.lib import helper_alpha','lib,helper_alpha');
INSERT INTO "external_imports" VALUES(11,'import json','json');
INSERT INTO "external_imports" VALUES(12,'from pkg.lib.alpha import helper_alpha, other_alpha','alpha,helper_alpha,other_alpha');
CREATE TABLE files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT NOT NULL UNIQUE,
        mtime_ms REAL NOT NULL,
        language TEXT NOT NULL,
        line_count INTEGER NOT NULL DEFAULT 0,
        symbol_count INTEGER NOT NULL DEFAULT 0,
        pagerank REAL NOT NULL DEFAULT 0.0
      , is_barrel INTEGER NOT NULL DEFAULT 0);
INSERT INTO "files" VALUES(1,'pkg/__init__.py',1.78954244982966894e+12,'python',1,0,2.09197839450470518e-01,0);
INSERT INTO "files" VALUES(2,'pkg/eta.py',1.78954548178440258e+12,'python',5,1,1.9887390942845326e-01,0);
INSERT INTO "files" VALUES(3,'pkg/gamma.py',1.78954244982966894e+12,'python',5,1,1.34529147982062786e-02,0);
INSERT INTO "files" VALUES(4,'pkg/lib/__init__.py',1.78954244982966894e+12,'python',4,0,1.29523345586629173e-01,1);
INSERT INTO "files" VALUES(5,'pkg/lib/alpha.py',1.78954244982966894e+12,'python',7,2,1.26082035579290097e-01,0);
INSERT INTO "files" VALUES(6,'pkg/lib/beta.py',1.78954244982966894e+12,'python',5,1,1.34529147982062786e-02,0);
INSERT INTO "files" VALUES(7,'pkg/sub/__init__.py',1.78954244982966894e+12,'python',1,0,1.29523345586629173e-01,0);
INSERT INTO "files" VALUES(8,'pkg/sub/delta.py',1.78954244982966894e+12,'python',5,1,1.26082035579290097e-01,0);
INSERT INTO "files" VALUES(9,'pkg/sub/eps.py',1.78954244982966894e+12,'python',5,1,1.34529147982062786e-02,0);
INSERT INTO "files" VALUES(10,'pkg/theta.py',1.78954659611133813e+12,'python',5,1,1.34529147982062786e-02,0);
INSERT INTO "files" VALUES(11,'pkg/zeta.py',1.78954244982966894e+12,'python',5,1,1.34529147982062786e-02,0);
INSERT INTO "files" VALUES(12,'tools/run-it.py',1.78954244982966894e+12,'python',5,1,1.34529147982062786e-02,0);
CREATE TABLE refs (
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        source_file_id INTEGER REFERENCES files(id) ON DELETE CASCADE,
        import_source TEXT
      );
INSERT INTO "refs" VALUES(2,'alpha',NULL,'import pkg.lib.alpha');
INSERT INTO "refs" VALUES(2,'helper_alpha',5,NULL);
INSERT INTO "refs" VALUES(3,'alpha',NULL,'import pkg.lib.alpha as alpha_mod');
INSERT INTO "refs" VALUES(3,'alpha_mod',NULL,NULL);
INSERT INTO "refs" VALUES(3,'gamma',NULL,NULL);
INSERT INTO "refs" VALUES(3,'helper_alpha',5,NULL);
INSERT INTO "refs" VALUES(4,'helper_alpha',NULL,'from .alpha import helper_alpha');
INSERT INTO "refs" VALUES(4,'alpha',NULL,NULL);
INSERT INTO "refs" VALUES(5,'helper_alpha',NULL,NULL);
INSERT INTO "refs" VALUES(5,'other_alpha',NULL,NULL);
INSERT INTO "refs" VALUES(6,'helper_alpha',NULL,'from .alpha import helper_alpha');
INSERT INTO "refs" VALUES(6,'alpha',NULL,NULL);
INSERT INTO "refs" VALUES(6,'beta_thing',NULL,NULL);
INSERT INTO "refs" VALUES(8,'lib',NULL,'from pkg.lib import alpha');
INSERT INTO "refs" VALUES(8,'alpha',NULL,'from pkg.lib import alpha');
INSERT INTO "refs" VALUES(8,'delta',NULL,NULL);
INSERT INTO "refs" VALUES(8,'helper_alpha',5,NULL);
INSERT INTO "refs" VALUES(9,'beta_thing',NULL,'from ..lib.beta import beta_thing');
INSERT INTO "refs" VALUES(9,'beta',NULL,NULL);
INSERT INTO "refs" VALUES(10,'lib',NULL,'from pkg.lib import helper_alpha');
INSERT INTO "refs" VALUES(10,'helper_alpha',NULL,'from pkg.lib import helper_alpha');
INSERT INTO "refs" VALUES(10,'theta',NULL,NULL);
INSERT INTO "refs" VALUES(11,'json',NULL,NULL);
INSERT INTO "refs" VALUES(11,'zeta',NULL,NULL);
INSERT INTO "refs" VALUES(11,'dumps',NULL,NULL);
INSERT INTO "refs" VALUES(12,'alpha',NULL,'from pkg.lib.alpha import helper_alpha, other_alpha');
INSERT INTO "refs" VALUES(12,'helper_alpha',NULL,'from pkg.lib.alpha import helper_alpha, other_alpha');
INSERT INTO "refs" VALUES(12,'other_alpha',NULL,'from pkg.lib.alpha import helper_alpha, other_alpha');
INSERT INTO "refs" VALUES(12,'main',NULL,NULL);
CREATE TABLE symbols (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        kind TEXT NOT NULL,
        line INTEGER NOT NULL,
        end_line INTEGER NOT NULL,
        is_exported INTEGER NOT NULL DEFAULT 0,
        signature TEXT
      , qualified_name TEXT);
INSERT INTO "symbols" VALUES(1,2,'eta','function',3,4,1,'def eta()',NULL);
INSERT INTO "symbols" VALUES(2,3,'gamma','function',3,4,1,'def gamma()',NULL);
INSERT INTO "symbols" VALUES(3,5,'helper_alpha','function',1,2,1,'def helper_alpha()',NULL);
INSERT INTO "symbols" VALUES(4,5,'other_alpha','function',5,6,1,'def other_alpha()',NULL);
INSERT INTO "symbols" VALUES(5,6,'beta_thing','function',3,4,1,'def beta_thing()',NULL);
INSERT INTO "symbols" VALUES(6,8,'delta','function',3,4,1,'def delta()',NULL);
INSERT INTO "symbols" VALUES(7,9,'eps','function',3,4,1,'def eps()',NULL);
INSERT INTO "symbols" VALUES(8,10,'theta','function',3,4,1,'def theta()',NULL);
INSERT INTO "symbols" VALUES(9,11,'zeta','function',3,4,1,'def zeta()',NULL);
INSERT INTO "symbols" VALUES(10,12,'main','function',3,4,1,'def main()',NULL);

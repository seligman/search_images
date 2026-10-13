/* Loads a static export (made by export.py) the way its pages do, and
   searches it: static/site.js runs against a made-up page whose <script>
   elements are read from the export folder, then static/search-core.js
   searches the records it hands over.

   Run by tests/test_export.py:
       node tests/js/export_test.js EXPORT_FOLDER EXPECTATIONS.json
   EXPECTATIONS.json: {"page": "index.html", "queries": [{"query": "...",
   "options": {...}, "titles": [titles of the matches, any order] or "count":
   how many, or "ordered": [titles, in this order]}],
   "details": {title: {key: value, ...}}, "count": records,
   "fail_once": a data file (such as "index-000.js") whose first load fails:
   the page must load the index on its second try, "vectors": how many
   pictures have a meaning vector (the similar option), "meaning": {query,
   id, dims} (the export ships its query model: the page embeds the query
   with it) or {query, state: "off"} (the page's self-check fails), "albums":
   the names of the albums the manifest lists (the marks option), in
   order, "trip": {title} (a trip's export, trip_export.py: its trip, whose
   days list every picture)}. A query with "with_albums" searches with those
   albums.
   Prints one line per test and exits with status 1 when any test fails.
*/
"use strict";

var assert = require("assert");
var crypto = require("crypto");
var fs = require("fs");
var path = require("path");
var vm = require("vm");

var STATIC = path.join(__dirname, "..", "..", "static");
var folder = process.argv[2];
var expected = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
var SearchCore = require(path.join(STATIC, "search-core.js"));

var XID = /^[a-z2-7]{12}$/;
var loaded = [];
var versions = {};          // file -> the ?v= it was asked for with
var failOnce = expected.fail_once ? "data/" + expected.fail_once : null;
var fetched = [];
// Nothing of an export is fetched (an older package's files would be).
global.fetch = function (url) {
    fetched.push(String(url));
    return Promise.reject(new Error("no fetch in this test"));
};

function sha1(file) {
    return crypto.createHash("sha1").update(fs.readFileSync(path.join(folder, file)))
        .digest("hex").slice(0, 10);
}

/* A page opened from disk: its settings block, and <script> elements that
   run the export's file (as a page script: "window" is the page, and so
   are its global names) once they are added to the head. */
function fakePage(htmlFile) {
    var html = fs.readFileSync(path.join(folder, htmlFile), "utf8");
    var match = /<script type="application\/json" id="app-config">([\s\S]*?)<\/script>/.exec(html);
    assert.ok(match, "the page has its settings block");
    var settings = match[1];
    var win = {location: {search: "", protocol: "file:", href: "file:///x/" + htmlFile}};
    win.window = win;
    var doc = {
        getElementById: function (id) {
            return id === "app-config" ? {textContent: settings} : null;
        },
        createElement: function (tag) {
            return {tagName: tag};
        },
        head: {
            appendChild: function (node) {
                setTimeout(function () {
                    var parts = String(node.src).split("?v=");
                    var file = path.join(folder, parts[0]);
                    var code;
                    try {
                        if (parts[0] === failOnce) {
                            failOnce = null;
                            throw new Error("made to fail once");
                        }
                        code = fs.readFileSync(file, "utf8");
                    } catch (error) {
                        if (node.onerror) {
                            node.onerror(error);
                        }
                        return;
                    }
                    loaded.push(parts[0]);
                    versions[parts[0]] = parts.length > 1 ? parts[1] : null;
                    if (/^(assets\/embed-|data\/meaning\/)/.test(parts[0])) {
                        // (The engine's scripts define page globals: EMBED,
                        // SEARCH_EMBED, EMBED_KERNEL.)
                        vm.runInThisContext(code, {filename: file});
                    } else {
                        var run = vm.runInThisContext("(function (window, SEARCH_DATA, " +
                                                      "SEARCH_VECTORS) {\n" + code + "\n})",
                                                      {filename: file});
                        run(win, win.SEARCH_DATA, win.SEARCH_VECTORS);
                    }
                    if (node.onload) {
                        node.onload();
                    }
                }, 0);
            }
        }
    };
    return {window: win, document: doc, settings: JSON.parse(settings)};
}

function loadSite(page) {
    var file = require.resolve(path.join(STATIC, "site.js"));
    delete require.cache[file];
    global.window = page.window;
    global.document = page.document;
    return require(file);
}

function exists(rel) {
    return typeof rel === "string" && rel.indexOf("..") < 0 && rel.charAt(0) !== "/" &&
        fs.existsSync(path.join(folder, decodeURIComponent(rel)));
}

function promised(start) {
    return new Promise(function (resolve) {
        start(resolve);
    });
}

var tests = [];

function test(name, body) {
    tests.push({name: name, body: body});
}

var page = fakePage(expected.page || "index.html");
var SITE = loadSite(page);
var data = null;
var byTitle = {};

if (failOnce) {
    test("a data file that failed to load is loaded again on the next try", function () {
        return promised(SITE.loadIndex).then(function (first) {
            assert.strictEqual(first, null, "the first try fails");
            assert.strictEqual(failOnce, null, "the file was asked for");
            assert.deepStrictEqual(fetched, [], "no older package was looked for");
            loaded = [];
        });
    });
}

test("the index loads from data scripts only", function () {
    return promised(SITE.loadIndex).then(function (loadedData) {
        assert.ok(loadedData, "loadIndex gave data");
        data = loadedData;
        var manifest = page.window.SEARCH_DATA_MANIFEST;
        assert.ok(manifest, "manifest.js ran");
        assert.strictEqual(data.records.length, manifest.count);
        if (expected.count !== undefined) {
            assert.strictEqual(data.records.length, expected.count);
        }
        if (!expected.fail_once) {
            assert.deepStrictEqual(loaded.slice(0, 1), ["data/manifest.js"]);
        }
        manifest.shards.forEach(function (name) {
            assert.ok(loaded.indexOf("data/" + name) >= 0, name);
        });
        assert.deepStrictEqual(fetched, []);
        assert.deepStrictEqual(data.libraries, [manifest.library]);
        data.records.forEach(function (rec) {
            byTitle[rec.t || ""] = rec;
        });
    });
});

test("data files are asked for with the hash of their contents", function () {
    var manifest = page.window.SEARCH_DATA_MANIFEST;
    assert.strictEqual(versions["data/manifest.js"], sha1("data/manifest.js"));
    assert.strictEqual(page.settings.dataVersion, sha1("data/manifest.js"));
    manifest.shards.forEach(function (name) {
        assert.strictEqual(manifest.versions[name], sha1("data/" + name), name);
        assert.strictEqual(versions["data/" + name], manifest.versions[name], name);
    });
    manifest.details.concat(manifest.locations ? [manifest.locations] : []).forEach(
        function (name) {
            assert.strictEqual(manifest.versions[name], sha1("data/" + name), name);
        });
});

test("records use export ids everywhere", function () {
    var ids = {};
    var library = page.window.SEARCH_DATA_MANIFEST.library.id;
    data.records.forEach(function (rec) {
        assert.ok(XID.test(rec.i), rec.i);
        assert.strictEqual(rec.l, library);
        ids[rec.i] = true;
        ["n", "f", "ts", "h"].forEach(function (key) {
            if (!expected.allow || expected.allow.indexOf(key) < 0) {
                assert.strictEqual(rec[key], undefined, key + " in " + rec.i);
            }
        });
    });
    data.records.forEach(function (rec) {
        if (rec.dg !== undefined) {
            assert.ok(ids[rec.dg], "the cover of a stack is exported: " + rec.dg);
        }
    });
});

test("every address of a record is a file of the export", function () {
    var caps = page.settings.capabilities;
    data.records.forEach(function (rec) {
        assert.ok(exists(SITE.thumbUrl(rec)), SITE.thumbUrl(rec));
        var image = SITE.imageUrl(rec);
        if (caps.images === false) {
            assert.strictEqual(image, null);
        } else {
            assert.ok(exists(image), image);
            assert.strictEqual(SITE.previewUrl(rec), image);
        }
        var pano = SITE.panoUrl(rec);
        if (pano !== null) {
            assert.ok(exists(pano), pano);
        }
        assert.ok(exists(SITE.detailUrl(rec.i).split("?")[0]));
    });
    assert.ok(exists(SITE.searchUrl({q: "x"}).split("?")[0]));
    var map = SITE.mapUrl({});
    assert.strictEqual(map !== null, Boolean(caps.map));
    if (map !== null) {
        assert.ok(exists(map));
    }
});

test("searches find the expected pictures", function () {
    (expected.queries || []).forEach(function (entry) {
        var options = entry.options || {};
        if (entry.with_albums) {
            options.albums = data.albums;
        }
        var result = SearchCore.search(data.records, entry.query, options);
        var label = JSON.stringify(entry.query) + " " + JSON.stringify(entry.options || {});
        if (entry.count !== undefined) {
            assert.strictEqual(result.items.length, entry.count, label);
            return;
        }
        if (entry.ordered) {
            assert.deepStrictEqual(result.items.map(function (id) {
                return data.records.filter(function (other) {
                    return other.i === id;
                })[0].t || "";
            }), entry.ordered, label);
            return;
        }
        var titles = result.items.map(function (id) {
            var rec = data.records.filter(function (other) {
                return other.i === id;
            })[0];
            return rec.t || "";
        }).sort();
        assert.deepStrictEqual(titles, entry.titles.slice().sort(), label);
    });
});

if (expected.albums !== undefined) {
    test("the albums of the manifest are those published", function () {
        assert.deepStrictEqual(data.albums.map(function (album) {
            return album.name;
        }), expected.albums);
        data.albums.forEach(function (album) {
            (album.items || []).forEach(function (id) {
                assert.ok(XID.test(id), id);
            });
        });
    });
}

test("details come from the detail shards", function () {
    var details = expected.details || {};
    return Promise.all(data.records.map(function (rec) {
        return promised(function (resolve) {
            SITE.loadRecord(rec.i, resolve);
        }).then(function (detail) {
            var want = details[rec.t || ""];
            if (detail !== null) {
                Object.keys(detail).forEach(function (key) {
                    assert.ok(["taken_at", "taken_source", "exif"].indexOf(key) >= 0, key);
                });
            }
            if (want === null) {
                assert.strictEqual(detail, null, rec.t);
            } else if (want) {
                assert.ok(detail, "details of " + rec.t);
                Object.keys(want).forEach(function (key) {
                    assert.deepStrictEqual(detail[key], want[key], rec.t + ": " + key);
                });
            }
        });
    })).then(function () {
        var shards = page.window.SEARCH_DATA_MANIFEST.details;
        shards.forEach(function (name) {
            assert.ok(exists("data/" + name), name);
        });
    });
});

test("locations only with the map", function () {
    return promised(SITE.loadLocations).then(function (points) {
        var manifest = page.window.SEARCH_DATA_MANIFEST;
        assert.ok(Array.isArray(points));
        if (!manifest.locations) {
            assert.deepStrictEqual(points, []);
            assert.ok(!exists("data/locations.js"));
            return;
        }
        var ids = {};
        data.records.forEach(function (rec) {
            ids[rec.i] = rec;
        });
        points.forEach(function (point) {
            assert.ok(ids[point.id], point.id);
            assert.strictEqual(ids[point.id].gp, 1);
            assert.strictEqual(Math.round(point.lat * 10000) / 10000, point.lat);
        });
        assert.strictEqual(points.length, (expected.locations || points.length));
    });
});

test("meaning vectors only with the similar option", function () {
    global.SearchCore = SearchCore;
    return promised(SITE.loadVectors).then(function (vectors) {
        delete global.SearchCore;
        var manifest = page.window.SEARCH_DATA_MANIFEST;
        if (!manifest.vectors) {
            assert.strictEqual(vectors, null);
            assert.ok(!fs.readdirSync(path.join(folder, "data")).some(function (name) {
                return /^vectors-/.test(name);
            }));
            assert.ok(!expected.vectors, "the export has no vectors");
            return;
        }
        assert.ok(vectors, "the vectors load");
        if (manifest.meaning) {
            // Its own model embeds queries; the vectors carry the model's id.
            assert.strictEqual(vectors.query, true);
            assert.strictEqual(vectors.model, manifest.meaning.id);
        } else {
            assert.strictEqual(vectors.query, false, "an export without its model cannot " +
                               "embed queries");
            assert.strictEqual(vectors.model, "", "the model's name is not published");
        }
        assert.strictEqual(vectors.ids.length, expected.vectors);
        manifest.vectors.forEach(function (name) {
            assert.strictEqual(manifest.versions[name], sha1("data/" + name), name);
            assert.strictEqual(versions["data/" + name], manifest.versions[name], name);
        });
        var known = {};
        data.records.forEach(function (rec) {
            known[rec.i] = true;
        });
        vectors.ids.forEach(function (id) {
            assert.ok(XID.test(id) && known[id], id);
        });
        // More like this works from the files alone.
        var index = SearchCore.prepare(data.records);
        assert.strictEqual(index.setVectors(vectors), vectors.ids.length);
        var result = index.search("similar:" + vectors.ids[0]);
        assert.strictEqual(result.items[0], vectors.ids[0]);
        assert.strictEqual(result.meaning.mode, "similar");
    }, function (error) {
        delete global.SearchCore;
        throw error;
    });
});

if (expected.meaning) {
    test("nothing of the model loads before a search by meaning", function () {
        assert.ok(SITE.meaningInBrowser());
        assert.deepStrictEqual(loaded.filter(function (name) {
            return /^(assets\/embed-|data\/meaning\/)/.test(name);
        }), []);
    });

    test("search by meaning runs in the page", function () {
        var manifest = page.window.SEARCH_DATA_MANIFEST;
        var progress = [];
        return promised(function (resolve) {
            SITE.queryVector(expected.meaning.query, resolve, function (step) {
                progress.push(step);
            });
        }).then(function (answer) {
            var state = expected.meaning.state || "ready";
            assert.strictEqual(answer.state, state, JSON.stringify(answer));
            ["assets/embed-kernel.js", "assets/embed-engine.js", "data/meaning/model.js"]
                .forEach(function (name) {
                    assert.ok(loaded.indexOf(name) >= 0, name);
                    assert.ok(versions[name], name + " is asked for with its hash");
                });
            assert.ok(progress.length >= 2);
            assert.strictEqual(progress[progress.length - 1].loaded,
                               progress[progress.length - 1].total);
            assert.strictEqual(progress[0].total, manifest.meaning.gzip);
            if (state === "off") {
                assert.strictEqual(SITE.meaningInBrowser(), false);
                return null;
            }
            assert.strictEqual(answer.model, expected.meaning.id);
            assert.strictEqual(answer.dims, expected.meaning.dims);
            assert.strictEqual(Buffer.from(answer.vec, "base64").length, expected.meaning.dims);
            // Asked again: from the engine at once, nothing more loaded.
            var count = loaded.length;
            return promised(function (resolve) {
                SITE.queryVector(expected.meaning.query, resolve);
            }).then(function (again) {
                assert.deepStrictEqual(again, answer);
                assert.strictEqual(loaded.length, count);
            });
        });
    });
}

if (expected.trip) {
    test("a trip's export: the trip, its days and their pictures", function () {
        return promised(SITE.loadTrips).then(function (found) {
            var manifest = page.window.SEARCH_DATA_MANIFEST;
            assert.ok(found && found.trips.length === 1, "one trip");
            assert.strictEqual(manifest.versions[manifest.trips], sha1("data/" + manifest.trips));
            assert.strictEqual(versions["data/" + manifest.trips],
                               manifest.versions[manifest.trips]);
            var trip = found.trips[0];
            assert.strictEqual(trip.title, expected.trip.title);
            assert.deepStrictEqual(trip.libraries, [manifest.library.id]);
            var ids = {};
            data.records.forEach(function (rec) {
                ids[rec.i] = true;
            });
            var listed = trip.other.slice();
            trip.days.forEach(function (day) {
                assert.strictEqual(day.gaps.length, Math.max(0, day.ids.length - 1));
                listed = listed.concat(day.ids);
            });
            listed.forEach(function (id) {
                assert.ok(ids[id], id);
            });
            assert.strictEqual(listed.length, data.records.length);
        });
    });
}

test("a picture's id that is not in the export has no details", function () {
    return promised(function (resolve) {
        SITE.loadRecord("aaaaaaaaaaaa", resolve);
    }).then(function (detail) {
        assert.strictEqual(detail, null);
    });
});

var passed = 0;

function run(index) {
    if (index >= tests.length) {
        console.log(passed + " of " + tests.length + " passed");
        process.exit(passed === tests.length ? 0 : 1);
        return;
    }
    var entry = tests[index];
    Promise.resolve().then(entry.body).then(function () {
        passed += 1;
        console.log("ok   " + entry.name);
    }, function (error) {
        console.log("FAIL " + entry.name + "\n     " + (error && error.stack || error));
    }).then(function () {
        run(index + 1);
    });
}

run(0);

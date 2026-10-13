/* Tests for camera RAW files in the pages (see raw_photos.py): a picture's RAW
   file in its information pane (static/viewer.js: VIEWER.format.rawText and
   rawUrl, and the pane itself), and the Export dialog's words about RAW
   files (static/export-review.js). The real page scripts on the made-up DOM
   of tests/js/app_meaning_test.js. Every picture here is made up.

   Run by tests/test_raw_photos.py, or directly:
       node tests/js/raw_test.js
   Prints one line per test and exits with status 1 when any test fails.
*/
"use strict";

var assert = require("assert");
var path = require("path");
var vm = require("vm");

var FAKE = require(path.join(__dirname, "app_meaning_test.js"));
var waitFor = FAKE.waitFor;
var settle = FAKE.settle;

var STATIC = path.join(__dirname, "..", "..", "static");
var VIEWER = require(path.join(STATIC, "viewer.js"));
var R = require(path.join(STATIC, "export-review.js"));

function records() {
    return [
        {i: 1, l: "lib", t: "Tower", n: "DSC0001.JPG", g: ["tower"], ds: 1,
         dk: "2021-06-13", ts: "2021-06-13T13:36:50", w: 4912, hgt: 3264},
        {i: 2, l: "lib", t: "Bridge", n: "DSC0002.ARW", g: ["bridge"], ds: 1,
         dk: "2021-06-13", ts: "2021-06-13T14:00:00", w: 1067, hgt: 1600}
    ];
}

function response(status, body) {
    var text = JSON.stringify(body);
    return Promise.resolve({
        ok: status >= 200 && status < 300,
        status: status,
        headers: {get: function () {
            return null;
        }},
        body: null,
        json: function () {
            return Promise.resolve().then(function () {
                return JSON.parse(text);
            });
        }
    });
}

/* The search page on the server (admin: the admin's). */
function serverPage(admin) {
    var data = {records: records()};
    var document = FAKE.makeDocument();
    document.createElementNS = function (ns, tag) {
        var node = document.createElement(tag);
        node.namespaceURI = ns;
        return node;
    };
    var config = {mode: "server", page: "search", api: "/api", tiles: "/tile",
                  adminApi: "/api/admin", libraries: ["Example"],
                  libraryList: [{id: "lib", name: "Example"}],
                  capabilities: {admin: admin, static: false}};
    FAKE.parseBody(document, FAKE.PAGE, config);
    function storage() {
        var items = {};
        return {
            getItem: function (key) {
                return Object.prototype.hasOwnProperty.call(items, key) ? items[key] : null;
            },
            setItem: function (key, value) {
                items[key] = String(value);
            },
            removeItem: function (key) {
                delete items[key];
            }
        };
    }
    function fetch(url) {
        if (String(url).split("?")[0] === "/api/index") {
            return response(200, {version: 1, libraries: [{id: "lib", name: "Example"}],
                                  albums: [], records: data.records});
        }
        return response(404, {error: "not here"});
    }
    var context = {
        document: document,
        navigator: {userAgent: "test", connection: {saveData: false}, clipboard: null},
        location: {search: "", hash: "", pathname: "/", protocol: "http:",
                   href: "http://127.0.0.1:5000/", origin: "http://127.0.0.1:5000"},
        history: {state: null, scrollRestoration: "auto", pushState: function (state) {
            this.state = state;
        }, replaceState: function (state) {
            this.state = state;
        }, back: function () {}},
        localStorage: storage(),
        sessionStorage: storage(),
        innerWidth: 1400,
        innerHeight: 900,
        scrollY: 0,
        devicePixelRatio: 1,
        scrollTo: function () {},
        scrollBy: function () {},
        matchMedia: function () {
            return {matches: false, addEventListener: function () {}, addListener: function () {}};
        },
        requestAnimationFrame: function (callback) {
            return setTimeout(callback, 0);
        },
        cancelAnimationFrame: clearTimeout,
        getComputedStyle: function () {
            return {getPropertyValue: function () {
                return "";
            }};
        },
        setTimeout: setTimeout,
        clearTimeout: clearTimeout,
        setInterval: setInterval,
        clearInterval: clearInterval,
        console: {log: function () {}, warn: function () {}, error: function () {}},
        atob: function (text) {
            return Buffer.from(text, "base64").toString("binary");
        },
        btoa: function (text) {
            return Buffer.from(text, "binary").toString("base64");
        },
        URLSearchParams: URLSearchParams,
        URL: URL,
        fetch: fetch
    };
    context.window = context;
    context.addEventListener = function () {};
    context.removeEventListener = function () {};
    vm.createContext(context);
    FAKE.SCRIPTS.forEach(function (script) {
        vm.runInContext(script.code, context, {filename: script.name});
    });
    return {context: context, document: document, data: data};
}

function loaded(page) {
    return waitFor(function () {
        return /\d+ pictures?/.test(FAKE.byId(page, "result-count").textContent);
    });
}

// The information pane of a record with a detail: {box, raw: the RAW row's value}.
function pane(page, rec, detail) {
    var box = page.document.createElement("div");
    page.document.body.appendChild(box);
    page.context.VIEWER.renderInfo(box, rec, detail, {get: function () {
        return null;
    }});
    var terms = box.querySelectorAll("dt");
    for (var k = 0; k < terms.length; k++) {
        if (terms[k].textContent === "RAW") {
            return {box: box, raw: terms[k].nextSibling};
        }
    }
    return {box: box, raw: null};
}

var tests = [];

function test(name, body) {
    tests.push({name: name, body: body});
}

test("a picture's RAW file, in words", function () {
    var text = VIEWER.format.rawText;
    assert.strictEqual(text({name: "DSC0001.ARW", size: 25165824, format: "ARW", how: "name"}),
                       "DSC0001.ARW (24 MB)");
    assert.strictEqual(text({name: "DSC0002.NEF", size: 9437184, format: "NEF", how: "time"}),
                       "DSC0002.NEF (9.0 MB), the same camera and second");
    // A published site without file names.
    assert.strictEqual(text({size: 1048576, format: "DNG", how: "name"}), "DNG file (1.0 MB)");
    assert.strictEqual(text({format: "<b>", how: "name"}), "RAW file");
    // A picture that is a RAW file.
    assert.strictEqual(text({name: "DSC0003.ARW", size: 17000000, format: "ARW", how: "alone",
                             preview: [1067, 1600], sensor: [6000, 4000]}),
                       "ARW file, shown by the camera's preview in it; the RAW file holds " +
                       "6000 \u00d7 4000");
    assert.strictEqual(text({how: "alone", preview: [1067, 1600], sensor: ["x", 2]}),
                       "RAW file, shown by the camera's preview in it");
    assert.strictEqual(text(null), "");
    assert.strictEqual(text("DSC0001.ARW"), "");
});

test("only the server's or the site's own links download it", function () {
    var url = VIEWER.format.rawUrl;
    assert.strictEqual(url({url: "/raw/12"}), "/raw/12");
    assert.strictEqual(url({url: "raws/abcdefgh2345.arw"}), "raws/abcdefgh2345.arw");
    ["javascript:alert(1)", "//example.invalid/x.arw", "/raw/12?x=1", "raws/../x.arw",
     "https://example.invalid/raws/a.arw", "/image/12", 12, null].forEach(function (value) {
        assert.strictEqual(url({url: value}), "", String(value));
    });
    assert.strictEqual(url(null), "");
});

test("the information pane shows the RAW file, with a link for the owner", function () {
    var page = serverPage(true);
    return loaded(page).then(function () {
        var found = pane(page, page.data.records[0], {
            id: 1, taken_at: "2021-06-13T13:36:50", taken_source: "exif", size: 3670016,
            raw: {name: "DSC0001.ARW", size: 16777216, format: "ARW", how: "name",
                  url: "/raw/1"}});
        assert.ok(found.raw, "no RAW row");
        assert.strictEqual(found.raw.querySelector(".info-raw").firstChild.textContent,
                           "DSC0001.ARW (16 MB)");
        var link = found.raw.querySelector("a.info-raw-link");
        assert.strictEqual(link.getAttribute("href") || link.href, "/raw/1");
        assert.strictEqual(link.textContent, "Download");
        assert.strictEqual(found.box.querySelector(".info-badge.is-raw"), null);
        // A picture that is a RAW file: a badge, and how it is shown.
        found = pane(page, page.data.records[1], {
            id: 2, size: 17825792,
            raw: {name: "DSC0002.ARW", size: 17825792, format: "ARW", how: "alone",
                  preview: [1067, 1600], sensor: [6000, 4000], url: "/raw/2"}});
        assert.ok(found.box.querySelector(".info-badge.is-raw"));
        assert.strictEqual(found.raw.querySelector(".info-raw").firstChild.textContent,
                           "ARW file, shown by the camera's preview in it; the RAW file holds " +
                           "6000 \u00d7 4000");
        // Without a link (a visitor): the words alone; without a RAW file: no row.
        found = pane(page, page.data.records[0], {
            id: 1, raw: {name: "DSC0001.ARW", size: 16777216, format: "ARW", how: "name"}});
        assert.strictEqual(found.raw.textContent, "DSC0001.ARW (16 MB)");
        assert.strictEqual(found.raw.querySelector("a"), null);
        assert.strictEqual(pane(page, page.data.records[0], {id: 1}).raw, null);
        assert.strictEqual(pane(page, page.data.records[0], null).raw, null);
        return settle(5);
    });
});

test("the Export dialog's words about RAW files", function () {
    assert.ok(R.OPTIONS.raw_files.label);
    assert.ok(/serial number/.test(R.OPTIONS.raw_files.hint));
    assert.strictEqual(R.rawsText(null, {raw_files: true}), "");
    assert.strictEqual(R.rawsText({total: 0}, {raw_files: true}), "");
    assert.strictEqual(R.rawsText({total: 3, included: 0, location: 0}, {raw_files: false}),
                       "3 pictures have RAW files; they stay home (RAW files publishes them).");
    assert.strictEqual(R.rawsText({total: 1, included: 0, location: 0}, {}),
                       "1 picture has a RAW file; it stays home (RAW files publishes them).");
    assert.strictEqual(R.rawsText({total: 3, included: 2, location: 1}, {raw_files: true}),
                       "2 of 3 RAW files go along as the camera wrote them, with every fact " +
                       "in them: the time to the second, the camera and its serial number; " +
                       "1 stays home: it holds a GPS position, and Location is off or its " +
                       "picture's own position is set aside.");
    assert.ok(/2 stay home: they hold a GPS position, and Location is off or their pictures' /
              .test(R.rawsText({total: 3, included: 1, location: 2}, {raw_files: true})));
    // A trip's export: one near home stays home too.
    assert.ok(/own position is set aside or near home\.$/.test(R.rawsText(
        {total: 3, included: 1, location: 2}, {raw_files: true, location: true, home_km: 10})));
    assert.ok(/own position is set aside\.$/.test(R.rawsText(
        {total: 3, included: 1, location: 2}, {raw_files: true, location: true, home_km: 0})));
    assert.ok(/RAW file, when it has one/.test(R.detailsText({raw_files: true})));
    assert.ok(!/RAW/.test(R.detailsText({})));
    assert.ok(/RAW files 2\.0 MB/.test(R.sizeText({size_estimate: 3000000, size_parts: {
        thumbs: 1000, raws: 2097152}})));
});

test("choosing pictures one by one counts their RAW files", function () {
    var reply = {options: {raw_files: true, exclude_ids: [], include_ids: [],
                           exclude_types: [], exclude_private: true,
                           exclude_sensitive: true, exclude_undescribed: true},
                 counts: {exported: 2, by_reason: {}, raws: {total: 2, included: 2,
                                                             location: 0}},
                 flagged: [{id: 5, reason: "writing", reasons: ["writing"], raw: "included",
                            bytes: 100, parts: {raws: 60, thumbs: 40}}],
                 size_estimate: 1000, size_parts: {raws: 600, thumbs: 400}};
    var options = JSON.parse(JSON.stringify(reply.options));
    options.exclude_ids = [5];
    var after = R.recount(reply, options);
    assert.deepStrictEqual(after.counts.raws, {total: 1, included: 1, location: 0});
    assert.strictEqual(after.size_parts.raws, 540);
});

/* ---------- Run ---------------------------------------------------------------------- */

var failed = 0;
tests.reduce(function (chain, entry) {
    return chain.then(function () {
        return Promise.resolve().then(entry.body).then(function () {
            console.log("ok   " + entry.name);
        }, function (error) {
            failed += 1;
            console.log("FAIL " + entry.name + "\n     " + (error && error.stack || error));
        });
    });
}, Promise.resolve()).then(function () {
    console.log((tests.length - failed) + " of " + tests.length + " passed");
    process.exit(failed ? 1 : 0);
});

/* Tests for times in the local time of the place in the pages (see
   local_times.py): the camera's clock under the time in a picture's
   information pane (static/viewer.js: VIEWER.format.clockText and the pane
   itself), and the Manage page's line about local times
   (static/place-names.js). The real page scripts on the made-up DOM of
   tests/js/app_meaning_test.js, with a fake fetch() for the server's
   answers. Every picture here is made up.

   Run by tests/test_local_times.py, or directly:
       node tests/js/local_times_test.js
   Prints one line per test and exits with status 1 when any test fails.
*/
"use strict";

var assert = require("assert");
var fs = require("fs");
var path = require("path");
var vm = require("vm");

var FAKE = require(path.join(__dirname, "app_meaning_test.js"));
var waitFor = FAKE.waitFor;
var settle = FAKE.settle;

var STATIC = path.join(__dirname, "..", "..", "static");
var VIEWER = require(path.join(STATIC, "viewer.js"));

function records() {
    return [
        {i: 1, l: "lib", t: "Harbour", g: ["harbour"], ds: 1, dk: "2023-07-01",
         ts: "2023-07-01T15:00:10", gp: 1},
        {i: 2, l: "lib", t: "Road", g: ["road"], ds: 1, dk: "2023-07-01",
         ts: "2023-07-01T11:15:30", mv: 1, dur: 60}
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

/* The search page on the server (admin: the admin's); page.log.places is the
   answer of the Manage page's /places. */
function serverPage(admin) {
    var log = {};
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
        var plain = String(url).split("?")[0];
        if (plain === "/api/index") {
            return response(200, {version: 1, libraries: [{id: "lib", name: "Example"}],
                                  albums: [], records: data.records});
        }
        if (plain === "/api/admin/places") {
            return response(200, log.places || {});
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
        console: {log: function () {}, warn: function () {}, error: function (text) {
            log.error = String(text);
        }},
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
    return {context: context, document: document, log: log, data: data};
}

function loaded(page) {
    return waitFor(function () {
        return /\d+ pictures?/.test(FAKE.byId(page, "result-count").textContent);
    });
}

// The "Taken" value of the information pane of a record with a detail.
function taken(page, rec, detail) {
    var box = page.document.createElement("div");
    page.document.body.appendChild(box);
    page.context.VIEWER.renderInfo(box, rec, detail, {get: function () {
        return null;
    }});
    var terms = box.querySelectorAll("dt");
    for (var k = 0; k < terms.length; k++) {
        if (terms[k].textContent === "Taken") {
            return terms[k].nextSibling;
        }
    }
    return null;
}

var tests = [];

function test(name, body) {
    tests.push({name: name, body: body});
}

test("the camera's clock, in words", function () {
    var text = VIEWER.format.clockText;
    assert.strictEqual(text("2023-07-01T11:15:30",
                            {taken_at: "2023-07-01T10:15:30", tz_offset: "+01:00",
                             kind: "track", zone: "Europe/Paris"}),
                       "Camera clock 10:15 (UTC+01:00); shown in the local time of its place " +
                       "(Europe/Paris), by the video's GPS track.");
    // Another day: its date too. No offset in the file: none said.
    assert.strictEqual(text("2023-07-01T03:00:10",
                            {taken_at: "2023-06-30T20:00:10", tz_offset: "", kind: "gps",
                             zone: "Asia/Tokyo"}),
                       "Camera clock Jun 30, 2023 at 20:00; shown in the local time of its " +
                       "place (Asia/Tokyo), by its GPS time.");
    // Only the offset changed.
    assert.strictEqual(text("2023-07-01T08:30:00",
                            {taken_at: "2023-07-01T08:30:00", tz_offset: "+02:00",
                             kind: "gps_date", zone: "America/Argentina/Ushuaia"}),
                       "Camera clock 08:30 (UTC+02:00); the UTC offset is its place's " +
                       "(America/Argentina/Ushuaia), by its GPS date.");
    // A visitor's (no zone), like another picture.
    assert.strictEqual(text("2023-07-01T15:00:10",
                            {taken_at: "2023-07-01T08:00:10", tz_offset: "", kind: "like"}),
                       "Camera clock 08:00; shown in the local time of its place, as for " +
                       "another picture from the same camera.");
    // Nothing to say, and odd values left out.
    assert.strictEqual(text("2023-07-01T15:00:10", null), "");
    assert.strictEqual(text("2023-07-01T15:00:10", {taken_at: "2023-07-01"}), "");
    assert.strictEqual(text("2023-07-01T15:00:10",
                            {taken_at: "2023-07-01T08:00:10", tz_offset: "<b>",
                             kind: "other", zone: "a zone with spaces"}),
                       "Camera clock 08:00; shown in the local time of its place.");
});

test("the information pane shows the camera's clock under the time", function () {
    var page = serverPage(false);
    return loaded(page).then(function () {
        var value = taken(page, page.data.records[0], {
            id: 1, taken_at: "2023-07-01T15:00:10", taken_source: "exif",
            tz_offset: "+09:00", lat: 35.0, lon: 139.0,
            clock: {taken_at: "2023-07-01T08:00:10", tz_offset: "", kind: "gps",
                    zone: "Asia/Tokyo"}});
        assert.ok(value, "no Taken row");
        assert.strictEqual(value.querySelector(".info-date").firstChild.textContent,
                           "Saturday, July 1, 2023 at 15:00 (UTC+09:00)");
        assert.strictEqual(value.querySelector(".info-clock-note").textContent,
                           "Camera clock 08:00; shown in the local time of its place " +
                           "(Asia/Tokyo), by its GPS time.");
        // A video's date says where it is from, then the clock.
        value = taken(page, page.data.records[1], {
            id: 2, media: "video", taken_at: "2023-07-01T11:15:30", taken_source: "video",
            tz_offset: "+02:00",
            clock: {taken_at: "2023-07-01T10:15:30", tz_offset: "+01:00", kind: "track"}});
        assert.strictEqual(value.querySelector(".info-date").firstChild.textContent,
                           "Saturday, July 1, 2023 at 11:15 (UTC+02:00; from the video)");
        assert.strictEqual(value.querySelector(".info-clock-note").textContent,
                           "Camera clock 10:15 (UTC+01:00); shown in the local time of its " +
                           "place, by the video's GPS track.");
        // Without a clock: the time alone, as before.
        value = taken(page, page.data.records[0], {
            id: 1, taken_at: "2023-07-01T15:00:10", taken_source: "exif", tz_offset: "+09:00"});
        assert.strictEqual(value.textContent, "Saturday, July 1, 2023 at 15:00 (UTC+09:00)");
        assert.strictEqual(value.querySelector(".info-clock-note"), null);
        return settle(5);
    });
});

function managePage(places) {
    var page = serverPage(true);
    page.log.places = places;
    var box = page.document.createElement("section");
    box.id = "place-names";
    page.document.body.appendChild(box);
    var code = fs.readFileSync(path.join(STATIC, "place-names.js"), "utf-8");
    return loaded(page).then(function () {
        vm.runInContext(code, page.context, {filename: "place-names.js"});
        return waitFor(function () {
            return box.querySelector(".place-names-status");
        });
    }).then(function () {
        var line = box.querySelector(".place-names-times");
        return line ? line.textContent : "";
    });
}

function placesAnswer(times, extra) {
    var answer = {available: true, cities: 3, built: null, rebuild: false, zones: true,
                  error: "", pictures: {located: 5, named: 5, region_only: 0, far: 0,
                                        pending: 0},
                  gps_ignored: 0, city_km: 25, max_km: 200, job: null,
                  estimates: {estimated: 0},
                  attribution_url: "https://example.invalid/a",
                  license_url: "https://example.invalid/l", local_times: times};
    Object.keys(extra || {}).forEach(function (key) {
        answer[key] = extra[key];
    });
    return answer;
}

test("the Manage page says how many pictures show the local time", function () {
    return managePage(placesAnswer({enabled: true, state: "done", local: 588, shifted: 573,
                                    offset_only: 15,
                                    kinds: {track: 551, gps: 8, gps_date: 8, like: 21}}))
        .then(function (text) {
            assert.strictEqual(text, "588 pictures show the local time of their place " +
                               "instead of the camera's clock (551 by their GPS track, 8 by " +
                               "their GPS time, 8 by their GPS date, 21 like other pictures " +
                               "from the same camera). For 15 of them only the UTC offset " +
                               "changed: the clock had the local time. The camera's clock " +
                               "is in each picture's details.");
            return managePage(placesAnswer({enabled: true, state: "done", local: 0,
                                            shifted: 0, offset_only: 0, kinds: {}}));
        }).then(function (text) {
            assert.ok(/^Every picture has its camera's time/.test(text), text);
            return managePage(placesAnswer({enabled: true, state: "no_zones", local: 0},
                                           {zones: false, rebuild: true}));
        }).then(function (text) {
            assert.ok(/^The place index has no time zones yet: the next scan/.test(text), text);
            return managePage(placesAnswer({enabled: true, state: "no_tzdata", local: 0}));
        }).then(function (text) {
            assert.ok(/pip install tzdata/.test(text), text);
            return managePage(placesAnswer({enabled: false, state: "off", local: 0}));
        }).then(function (text) {
            assert.ok(/^Local times are off/.test(text), text);
            // Without place names there is nothing to say about them.
            return managePage(placesAnswer({enabled: true, state: "no_places", local: 0},
                                           {available: false}));
        }).then(function (text) {
            assert.strictEqual(text, "");
            return settle(5);
        });
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

/* Tests for static/export-review.js: the words and rules of the Manage
   page's Export dialog (what the options mean, which pictures they leave
   out, choosing pictures one by one, and the summaries).

   Run by tests/test_admin_export.py with a JSON file that lists what
   export.py knows (its options, the picture types, the reasons it gives),
   or directly:
       node tests/js/export_review_test.js [expected.json]
   Prints one line per test and exits with status 1 when any test fails.
*/
"use strict";

var assert = require("assert");
var fs = require("fs");
var path = require("path");

var R = require(path.join(__dirname, "..", "..", "static", "export-review.js"));
var expected = process.argv[2] ? JSON.parse(fs.readFileSync(process.argv[2], "utf8")) : null;

var tests = [];

function test(name, body) {
    tests.push({name: name, body: body});
}

function defaults() {
    return {title: "", images: "web", location: false, file_names: false, folders: false,
            exclude_types: ["screenshot", "document"], exclude_private: true,
            exclude_sensitive: true, exclude_undescribed: true, include_ids: [],
            exclude_ids: [], credits: "", zip: false, allow_indexing: false, similar: true,
            meaning: true};
}

var SHOT = {id: 4, reason: "private", reasons: ["private", "screenshot"], title: "Chat"};
var SCAN = {id: 7, reason: "scan", reasons: ["scan"], title: "Old letter"};
var BLANK = {id: 9, reason: "undescribed", reasons: ["undescribed"], title: ""};
var RACY = {id: 2, reason: "sensitive", reasons: ["sensitive"], title: "Beach"};
var CHOSEN = {id: 11, reason: "included", reasons: [], title: "Sunset"};
var MENU = {id: 13, reason: "writing", reasons: ["writing"], title: "Menu"};
var OLD = {id: 15, reason: "unchecked", reasons: ["unchecked"], title: "Old one"};
var RECEIPT = {id: 17, reason: "document", reasons: ["document"], title: "Receipt"};

/* ---------- The words cover what export.py knows ---------------------------- */

test("every option has words", function () {
    var keys = Object.keys(R.OPTIONS).sort();
    if (expected) {
        assert.deepStrictEqual(keys, expected.options.filter(function (key) {
            return key !== "include_ids" && key !== "exclude_ids";
        }).sort());
    }
    ["web", "original", "none"].forEach(function (mode) {
        assert.ok(R.OPTIONS.images[mode].label && R.OPTIONS.images[mode].hint, mode);
    });
    keys.forEach(function (key) {
        if (key !== "images") {
            assert.ok(R.OPTIONS[key].label && R.OPTIONS[key].hint, key);
        }
    });
});

test("the picture types offered and every reason have words", function () {
    Object.keys(R.TYPES).forEach(function (type) {
        assert.ok(R.TYPES[type].label && R.TYPES[type].hint, type);
        if (expected) {
            assert.ok(expected.photo_types.indexOf(type) >= 0, type);
        }
    });
    var reasons = expected ? expected.reasons : ["private", "sensitive", "undescribed"];
    reasons.forEach(function (reason) {
        var words = R.REASONS[reason];
        assert.ok(words && words.one && words.many, reason);
    });
    // Words the owner sees never contain markup.
    JSON.stringify([R.OPTIONS, R.TYPES, R.REASONS]).replace(/[<>]/g, function (found) {
        // Only the zip hint names a file pattern with angle brackets.
        assert.ok(R.OPTIONS.zip.hint.indexOf(found) >= 0, found);
    });
});

/* ---------- Which pictures are published ------------------------------------ */

test("the options decide, unless a picture was chosen", function () {
    var options = defaults();
    assert.deepStrictEqual(R.activeReasons(SHOT, options), ["private", "screenshot"]);
    assert.strictEqual(R.published(SHOT, options), false);
    assert.strictEqual(R.published(SCAN, options), true);
    assert.strictEqual(R.published(BLANK, options), false);
    assert.strictEqual(R.published(CHOSEN, options), true);
    options.exclude_private = false;
    assert.deepStrictEqual(R.activeReasons(SHOT, options), ["screenshot"]);
    options.exclude_types = [];
    assert.strictEqual(R.published(SHOT, options), true);
    options.exclude_undescribed = false;
    assert.strictEqual(R.published(BLANK, options), true);
    options.exclude_ids = [9];
    assert.strictEqual(R.published(BLANK, options), false);
    options = defaults();
    options.include_ids = [9];
    assert.strictEqual(R.published(BLANK, options), true);
    // Pictures with writing are only shown; older descriptions count as
    // not checked, like pictures not described yet.
    options = defaults();
    assert.strictEqual(R.published(MENU, options), true);
    assert.deepStrictEqual(R.activeReasons(OLD, options), ["unchecked"]);
    assert.strictEqual(R.published(OLD, options), false);
    options.exclude_undescribed = false;
    assert.strictEqual(R.published(OLD, options), true);
});

test("a picture chosen despite its flags is left out again by a new one", function () {
    var options = defaults();
    // Published while it was not described yet ...
    options.include_ids = [[9, ["undescribed"]]];
    assert.strictEqual(R.published(BLANK, options), true);
    assert.deepStrictEqual(R.newFlags(BLANK, options), []);
    // ... then described: it shows private information.
    var now = {id: 9, reason: "private", reasons: ["private", "document"], title: "Statement"};
    assert.strictEqual(R.published(now, options), false);
    assert.deepStrictEqual(R.newFlags(now, options), ["private", "document"]);
    // Choosing it again keeps its flags of now.
    var again = R.choose(options, now, true);
    assert.deepStrictEqual(again.include_ids, [[9, ["private", "document"]]]);
    assert.strictEqual(R.published(now, again), true);
    assert.deepStrictEqual(R.newFlags(now, again), []);
    // A flag that the options do not leave out does not count.
    options.exclude_private = false;
    options.exclude_types = [];
    assert.strictEqual(R.published(now, options), true);
    // A plain id (from the API or an older version): whatever the flags.
    options = defaults();
    options.include_ids = [9];
    assert.strictEqual(R.published(now, options), true);
    assert.deepStrictEqual(R.newFlags(now, options), []);
});

test("choices are counted without asking the server again", function () {
    var options = defaults();
    var reply = {
        options: options,
        counts: {total: 10, exported: 5, needs_folder: 5, unavailable: 5,
                 by_reason: {"private": 1, undescribed: 1, sensitive: 1, hidden: 2}},
        size_estimate: 1000,
        size_parts: {thumbs: 100, images: 800, panos: 0, data: 100},
        flagged: [
            {id: 4, reasons: ["private", "screenshot"], excluded: true, bytes: 50,
             parts: {thumbs: 10, images: 40}, needs_folder: true},
            {id: 7, reasons: ["scan"], excluded: false, bytes: 70,
             parts: {thumbs: 10, images: 60}, needs_folder: true},
            {id: 9, reasons: ["undescribed"], excluded: true, bytes: 30,
             parts: {thumbs: 10, images: 20}, needs_folder: false}
        ]
    };
    assert.deepStrictEqual(R.recount(reply, options).counts, reply.counts);
    var chosen = R.choose(R.choose(options, reply.flagged[0], true), reply.flagged[1], false);
    var after = R.recount(reply, chosen);
    assert.strictEqual(after.counts.exported, 5);
    assert.deepStrictEqual(after.counts.by_reason, {undescribed: 1, sensitive: 1, hidden: 2,
                                                    excluded: 1});
    assert.strictEqual(after.size_estimate, 1000 + 50 - 70);
    assert.deepStrictEqual(after.size_parts, {thumbs: 100, images: 780, panos: 0, data: 100});
    assert.strictEqual(after.counts.needs_folder, 5);
    chosen = R.choose(chosen, reply.flagged[2], true);
    after = R.recount(reply, chosen);
    assert.strictEqual(after.counts.exported, 6);
    assert.strictEqual(after.size_estimate, 1000 + 50 - 70 + 30);
    assert.ok(!("undescribed" in after.counts.by_reason));
    // The reply itself is not changed.
    assert.strictEqual(reply.counts.exported, 5);
    assert.strictEqual(reply.counts.by_reason["private"], 1);
});

test("choosing a picture only lists it when the rules decide otherwise", function () {
    var options = defaults();
    var chosen = R.choose(options, SHOT, true);
    assert.deepStrictEqual(chosen.include_ids, [[4, ["private", "screenshot"]]]);
    assert.deepStrictEqual(chosen.exclude_ids, []);
    assert.strictEqual(R.published(SHOT, chosen), true);
    // The options passed in are not changed.
    assert.deepStrictEqual(options.include_ids, []);
    var back = R.choose(chosen, SHOT, false);
    assert.deepStrictEqual(back.include_ids, []);
    assert.deepStrictEqual(back.exclude_ids, []);
    assert.strictEqual(R.published(SHOT, back), false);
    // A scan is published by the rules: leaving it out lists it.
    var out = R.choose(back, SCAN, false);
    assert.deepStrictEqual(out.exclude_ids, [7]);
    assert.strictEqual(R.published(SCAN, out), false);
    out = R.choose(R.choose(out, RACY, true), BLANK, true);
    assert.deepStrictEqual(out.include_ids, [[2, ["sensitive"]], [9, ["undescribed"]]]);
    // A plain id of an older version is replaced, not doubled.
    var older = defaults();
    older.include_ids = [9, 12];
    assert.deepStrictEqual(R.choose(older, BLANK, true).include_ids,
                           [[9, ["undescribed"]], 12]);
    assert.deepStrictEqual(R.choose(out, SCAN, true).exclude_ids, []);
    assert.strictEqual(out.images, "web");
});

/* ---------- The review grid and the summaries -------------------------------- */

test("flagged pictures are grouped, most serious first", function () {
    var groups = R.groups([BLANK, SCAN, CHOSEN, SHOT, RACY, MENU, OLD, RECEIPT]);
    assert.deepStrictEqual(groups.map(function (group) {
        return group.key;
    }), ["private", "sensitive", "document", "scan", "writing", "unchecked", "undescribed",
         "chosen"]);
    groups.forEach(function (group) {
        assert.ok(group.title && group.hint, group.key);
    });
    assert.deepStrictEqual(groups[0].items, [SHOT]);
    assert.strictEqual(groups[0].title, "Private information");
    assert.ok(groups[0].hint);
    assert.deepStrictEqual(R.groups([]), []);
    assert.strictEqual(R.reasonWords(SHOT.reasons), "private information, screenshot");
    assert.strictEqual(R.reasonWords(["meme", "odd"]), "meme, odd");
});

test("counts, size and details in words", function () {
    var counts = {total: 1699, exported: 312, by_reason: {
        undescribed: 1204, screenshot: 31, hidden: 3, "private": 1, excluded: 2, failed: 4,
        unchecked: 1}};
    assert.strictEqual(R.headline(counts), "312 of 1,699 pictures will be published.");
    assert.deepStrictEqual(R.leftOut(counts), [
        "1 picture with private information", "31 screenshots",
        "1 picture described by an older version (not checked)",
        "1,204 pictures not described yet", "2 pictures you left out",
        "3 hidden pictures (never published)", "4 failed"]);
    assert.strictEqual(R.headline({total: 0, exported: 0}),
                       "This library has no pictures to publish yet.");
    assert.ok(/No picture/.test(R.headline({total: 4, exported: 0})));
    assert.strictEqual(R.sizeText({size_estimate: 170000000, size_parts: {
        thumbs: 12000000, images: 150000000, panos: 0, data: 8000000}}),
        "About 162 MB: thumbnails 11 MB, pictures 143 MB, pages and data 7.6 MB.");
    assert.strictEqual(R.sizeText({size_estimate: 2048, size_parts: {data: 2048}}),
                       "About 2.0 KB.");
    assert.strictEqual(R.sizeText(null), "");
    assert.strictEqual(R.bytes(512), "512 bytes");
    var options = defaults();
    assert.strictEqual(R.detailsText(options), "Published with each picture: its title, " +
        "description, tags, any text read in the picture, the day it was taken (from the " +
        "camera or the file name) and camera model and settings. Not its file name, " +
        "folder or location (GPS position and place names).");
    options.file_names = options.folders = true;
    assert.ok(/Not its location \(GPS position and place names\)\.$/.test(
        R.detailsText(options)), R.detailsText(options));
    options.location = true;
    assert.ok(/file name, folder and location \(GPS position and place names\)\.$/.test(
        R.detailsText(options)));
    // The owner's marks are named only when they go.
    options.marks = true;
    assert.ok(/and favorite star, rating and albums\.$/.test(R.detailsText(options)),
              R.detailsText(options));
    options.marks = false;
    // An export limited to an album says so.
    assert.strictEqual(R.headline({total: 20, exported: 18, album: {id: 3, name: "Trip"}}),
                       "18 of the 20 pictures of the album \u201cTrip\u201d will be published.");
    assert.strictEqual(R.headline({total: 0, exported: 0, album: {id: 3, name: "Trip"}}),
                       "The album has no pictures in this library.");
    assert.ok(R.OPTIONS.album.label && R.OPTIONS.marks.hint);
    // The Location option says that it publishes the place names.
    assert.ok(/town, region and country/.test(R.OPTIONS.location.hint));
    // A trip's story goes with the location only (it names the trip's places).
    assert.ok(/^With Location/.test(R.OPTIONS.story.hint));
    options = defaults();
    options.story = true;
    assert.ok(!/story/.test(R.detailsText(options)), R.detailsText(options));
    options.location = true;
    assert.ok(/The trip's story goes too, a paragraph a day, without the sentences that name /
        .test(R.detailsText(options)), R.detailsText(options));
    // Only this computer can open the site the command serves (export.py's
    // own server, which lets videos be moved in): the export's own command.
    assert.strictEqual(R.serveCommand("C:\\Photos\\Family"),
                       "python export.py --serve \"C:\\Photos\\Family\"");
    assert.strictEqual(R.serveCommand("C:\\Photos\\Family",
                                      {serve: "python \"program/export.py\" --serve x"}),
                       "python \"program/export.py\" --serve x");
});

test("search by meaning in the browser: its size, or why it cannot go", function () {
    var options = defaults();
    var counts = {total: 10, exported: 4, meaning: {available: true, reason: "",
                                                    bytes: 34603008, gzip: 25690112}};
    assert.strictEqual(R.meaningText(counts, options), "Search by meaning in the browser: " +
        "33 MB of model files; visitors download about 25 MB once, the first time they " +
        "search (works offline and from disk).");
    counts.meaning = {available: false, reason: "Set embedding.license_file in config.json."};
    assert.strictEqual(R.meaningText(counts, options), "Search by meaning in the browser is " +
        "not included: Set embedding.license_file in config.json.");
    options.meaning = false;
    assert.strictEqual(R.meaningText(counts, options), "");
    assert.strictEqual(R.meaningText({total: 1, exported: 1}, defaults()), "");
    assert.ok(/search by meaning 33 MB/.test(R.sizeText({size_estimate: 34700000, size_parts: {
        thumbs: 96992, meaning: 34603008}})));
    assert.ok(/once, the first time they search/.test(R.OPTIONS.meaning.hint));
});

test("More like this in words, and its vectors in the counts", function () {
    var options = defaults();
    assert.strictEqual(options.similar, true);
    var counts = {total: 10, exported: 4, similar: {vectors: 4, stale: 0, missing: 0}};
    assert.strictEqual(R.similarText(counts, options),
                       "More like this goes by meaning for every published picture.");
    counts.similar = {vectors: 3, stale: 1, missing: 0};
    assert.ok(/for 3 of 4 pictures; 1 has no up-to-date meaning vector/.test(
        R.similarText(counts, options)), R.similarText(counts, options));
    counts.similar = {vectors: 0, stale: 0, missing: 4};
    assert.ok(/No published picture has a meaning vector yet/.test(
        R.similarText(counts, options)));
    options.similar = false;
    assert.strictEqual(R.similarText(counts, options), "");
    assert.strictEqual(R.similarText({total: 1, exported: 1}, defaults()), "");
    assert.ok(/meaning vectors 3\.4 KB/.test(R.sizeText({size_estimate: 5000, size_parts: {
        thumbs: 1500, vectors: 3500}})));
    // A picture chosen in the review takes its vector along.
    var reply = {options: defaults(), counts: {total: 3, exported: 1, by_reason: {"private": 1},
                                                 similar: {vectors: 1, stale: 0, missing: 0}},
                 size_estimate: 100, size_parts: {thumbs: 50, vectors: 50},
                 flagged: [{id: 2, reasons: ["private"], excluded: true, bytes: 80,
                            parts: {thumbs: 30, vectors: 50}}]};
    var after = R.recount(reply, R.choose(reply.options, reply.flagged[0], true));
    assert.deepStrictEqual(after.counts.similar, {vectors: 2, stale: 0, missing: 0});
    assert.strictEqual(after.size_parts.vectors, 100);
    assert.deepStrictEqual(reply.counts.similar, {vectors: 1, stale: 0, missing: 0});
});

test("a video chosen in the review counts in the video sentence", function () {
    var options = defaults();
    options.videos = "include";
    var reply = {options: options,
                 counts: {total: 4, exported: 2, by_reason: {sensitive: 2},
                          videos: {total: 2, included: 1, not_playable: 1, too_large: 0}},
                 size_estimate: 100, size_parts: {thumbs: 50, videos: 50},
                 flagged: [{id: 20, reasons: ["sensitive"], excluded: true, bytes: 900,
                            parts: {thumbs: 20, videos: 880}, video: "included"},
                           {id: 21, reasons: ["sensitive"], excluded: true, bytes: 20,
                            parts: {thumbs: 20}, video: "too_large"}]};
    assert.ok(/^1 of 2 videos are published as a video;/.test(
        R.videoText(reply.counts.videos, options)));
    var chosen = R.choose(options, reply.flagged[0], true);
    var after = R.recount(reply, chosen);
    assert.deepStrictEqual(after.counts.videos,
                           {total: 3, included: 2, not_playable: 1, too_large: 0});
    assert.ok(/^2 of 3 videos are published as videos;/.test(
        R.videoText(after.counts.videos, options)), R.videoText(after.counts.videos, options));
    assert.strictEqual(after.size_parts.videos, 930);
    after = R.recount(reply, R.choose(chosen, reply.flagged[1], true));
    assert.deepStrictEqual(after.counts.videos,
                           {total: 4, included: 2, not_playable: 1, too_large: 1});
    // Left out again: back to the server's numbers.
    after = R.recount(reply, R.choose(chosen, reply.flagged[0], false));
    assert.deepStrictEqual(after.counts.videos, reply.counts.videos);
    // Videos not included: a poster, counted in the total only.
    var posters = {options: defaults(), counts: {total: 2, exported: 1, by_reason: {sensitive: 1},
                                                 videos: {total: 0, included: 0,
                                                          not_playable: 0, too_large: 0}},
                   size_estimate: 10, size_parts: {thumbs: 10},
                   flagged: [{id: 5, reasons: ["sensitive"], excluded: true, bytes: 20,
                              parts: {thumbs: 20}, video: "poster"}]};
    after = R.recount(posters, R.choose(posters.options, posters.flagged[0], true));
    assert.deepStrictEqual(after.counts.videos,
                           {total: 1, included: 0, not_playable: 0, too_large: 0});
    assert.ok(/^The video to publish is published as its poster frame/.test(
        R.videoText(after.counts.videos, posters.options)));
    // The reply itself is not changed.
    assert.deepStrictEqual(reply.counts.videos,
                           {total: 2, included: 1, not_playable: 1, too_large: 0});
});

/* ---------- Run ------------------------------------------------------------------ */

var failed = 0;
tests.forEach(function (item) {
    try {
        item.body();
        console.log("ok   " + item.name);
    } catch (error) {
        failed += 1;
        console.log("FAIL " + item.name + "\n" + (error && error.stack || error));
    }
});
console.log((tests.length - failed) + " of " + tests.length + " passed");
process.exit(failed ? 1 : 0);

/* Tests for how the job panel names and sums up People's jobs (Find faces, Group
   faces into people, Delete face data, a scan's faces step) and what the Manage
   page says of a library's faces in static/shell.js, for person: in
   static/search-core.js (the pictures of a person of /api/people), and for the
   moments of a video's people in static/viewer.js (where a video found by a
   person starts -- by the search the pages make for a person, or as the search
   page understood the words --, how a moment is said, and moving it there),
   under Node (no packages needed).

   Run by tests/test_people.py, or directly:
       node tests/js/people_test.js
   Prints one line per test and exits with status 1 when any test fails.
*/
"use strict";

var assert = require("assert");
var fs = require("fs");
var path = require("path");
var vm = require("vm");

var STATIC = path.join(__dirname, "..", "..", "static");
var tests = [];
var failures = 0;

function test(name, body) {
    tests.push({name: name, body: body});
}

function loadShell() {
    var noop = function () {};
    var context = {
        window: {},
        document: {documentElement: {setAttribute: noop, removeAttribute: noop},
                   addEventListener: noop},
        localStorage: {getItem: function () {
            return null;
        }, setItem: noop}
    };
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(path.join(STATIC, "shell.js"), "utf8") +
                    "\nthis.SHELL = SHELL;", context);
    return context.SHELL;
}

test("the faces job is named and summed up", function () {
    var jobs = loadShell().jobs;
    var job = {kind: "faces", library_id: "lib_a", library: "Family", state: "done",
               summary: {looked: 120, faces: 45, with_faces: 30, failed: 2, offline: 3,
                         stopped: false}};
    assert.strictEqual(jobs.title(job), "Find faces: Family");
    assert.strictEqual(jobs.stage("faces", job), "Finding faces");
    assert.strictEqual(jobs.summary(job), "45 faces found in 120 pictures looked at \u00b7 " +
                       "2 could not be looked at \u00b7 3 wait for their folder");
    job.params = {retry_failed: true};
    assert.strictEqual(jobs.title(job), "Retry finding faces: Family");
    job.summary = {looked: 0, faces: 0, failed: 0};
    assert.strictEqual(jobs.summary(job), "0 faces found in 0 pictures looked at");
    assert.strictEqual(jobs.title({kind: "faces", library_id: null, params: {}}),
                       "Find faces: all libraries");
});

test("deleting the face data is one job for every library", function () {
    var jobs = loadShell().jobs;
    var job = {kind: "delete_faces", library_id: null, state: "done", params: {},
               summary: {pictures: 10, faces: 1, removed: true}};
    assert.strictEqual(jobs.title(job), "Delete face data");
    assert.strictEqual(jobs.stage("delete faces", job), "Deleting face data");
    assert.strictEqual(jobs.summary(job), "face data deleted (1 face)");
});

test("a scan says what its faces step found (only when it looked)", function () {
    var jobs = loadShell().jobs;
    var scanned = {kind: "scan", library_id: "lib_a", library: "Family", state: "done",
                   summary: {libraries: [{files: 40, added: 3}],
                             faces: {looked: 3, faces: 4, with_faces: 2, failed: 0}}};
    assert.ok(/4 faces found in 3 pictures looked at/.test(jobs.summary(scanned)),
              jobs.summary(scanned));
    scanned.summary.faces = {looked: 0, faces: 0, failed: 0};
    assert.ok(!/face/.test(jobs.summary(scanned)), jobs.summary(scanned));
});

test("grouping faces into people is named and summed up", function () {
    var jobs = loadShell().jobs;
    var job = {kind: "group_faces", library_id: null, state: "done", params: {},
               summary: {faces: 120, grouped: 90, people: 7, "new": 2}};
    assert.strictEqual(jobs.title(job), "Group faces into people");
    assert.strictEqual(jobs.stage("people", job), "Grouping faces into people");
    assert.strictEqual(jobs.summary(job), "7 people (90 of 120 faces; 2 new)");
    job.summary = {faces: 1, grouped: 1, people: 1, "new": 0};
    assert.strictEqual(jobs.summary(job), "1 person (1 of 1 face)");
    // The faces job groups what it found: its summary says so too.
    var faces = {kind: "faces", library_id: null, state: "done", params: {},
                 summary: {looked: 10, faces: 4, failed: 0,
                           people: {faces: 4, grouped: 3, people: 1, "new": 1}}};
    assert.strictEqual(jobs.summary(faces), "4 faces found in 10 pictures looked at \u00b7 " +
                       "1 person (3 of 4 faces; 1 new)");
    var deleted = {kind: "delete_faces", library_id: null, state: "done", params: {},
                   summary: {pictures: 10, faces: 4, people: 2, named: 1, removed: true}};
    assert.strictEqual(jobs.summary(deleted), "face data deleted (4 faces, 2 people)");
});

test("the faces job says how many videos it looked at in frames", function () {
    var jobs = loadShell().jobs;
    var job = {kind: "faces", library_id: null, state: "done", params: {},
               summary: {looked: 12, faces: 9, failed: 1, videos: 3, frames: 27, no_tools: 0}};
    assert.strictEqual(jobs.summary(job), "9 faces found in 12 pictures looked at \u00b7 " +
                       "3 videos in 27 frames \u00b7 1 could not be looked at");
    job.summary = {looked: 0, faces: 0, failed: 0, videos: 0, frames: 0, no_tools: 2};
    assert.strictEqual(jobs.summary(job), "0 faces found in 0 pictures looked at \u00b7 " +
                       "2 videos wait for ffmpeg");
});

var VIEWER = require(path.join(STATIC, "viewer.js"));

test("a video found by a search for one of its people starts where that one is", function () {
    var moment = VIEWER.format.personMoment;
    var names = [{id: 3, label: "Ann Lee", named: true, at: [12.5, 30]},
                 {id: 4, label: "Bo", named: true, at: [4]},
                 {id: 9, label: "Person 9", named: false, at: [2]},
                 {id: 5, label: "Cy", named: true}];
    assert.strictEqual(moment(names, "person:\"Ann Lee\" beach"), 12.5);
    assert.strictEqual(moment(names, "person:bo"), 4);
    // (The search page gives its parsed query.)
    assert.strictEqual(moment(names, {text: "person:bo beach", clauses: []}), 4);
    assert.strictEqual(moment(names, "ann lee at the beach"), 12.5);
    assert.strictEqual(moment(names, "BO, swimming"), 4);
    assert.strictEqual(moment(names, "person:#9"), 2);
    // (The first of its people the search names; an unnamed one only by number.)
    assert.strictEqual(moment(names, "bo ann lee"), 12.5);
    assert.strictEqual(moment(names, "person 9"), null);
    // Not a part of a name, nor a person without moments, nor nobody.
    assert.strictEqual(moment(names, "ann"), null);
    assert.strictEqual(moment(names, "bob"), null);
    assert.strictEqual(moment(names, "person:cy"), null);
    assert.strictEqual(moment(names, ""), null);
    assert.strictEqual(moment(null, "bo"), null);
    assert.strictEqual(moment([{id: 6, label: "Zo\u00eb", named: true, at: [7]}], "zoe"), 7);
});

test("a video found by the search the pages make for a person starts where that one is",
     function () {
         var moment = VIEWER.format.personMoment;
         var names = [{id: 3, label: "Ann Lee", named: true, at: [12.5, 30]},
                      {id: 9, label: "Person 9", named: false, at: [2]},
                      {id: 4, label: "D'Arcy", named: true, at: [7]},
                      {id: 8, label: "J. R. Smith", named: true, at: [11]}];
         // The People page's "Pictures" and the viewer's chips: SearchCore.personTerm.
         var people = names.map(function (person) {
             return {id: person.id, label: person.label, name: person.named ? person.label : "",
                     ids: [1]};
         });
         names.forEach(function (person, index) {
             var term = SearchCore.personTerm(people, people[index]);
             assert.strictEqual(moment(names, term), person.at[0], term);
             // (As the search page gives it: parsed.)
             assert.strictEqual(moment(names, SearchCore.parse(term + " beach")), person.at[0],
                                term);
         });
         assert.strictEqual(moment(names, "person:\"Person 9\""), 2);
         assert.strictEqual(moment(names, "person:\u201cAnn Lee\u201d"), 12.5);
         // Names with punctuation, as words too; the start of a name as search takes it.
         assert.strictEqual(moment(names, "d'arcy at the beach"), 7);
         assert.strictEqual(moment(names, "person:\"D'Arcy\""), 7);
         assert.strictEqual(moment(names, "j. r. smith"), 11);
         assert.strictEqual(moment(names, "person:an"), 12.5);
         // Not a person the search leaves out, nor a part of a name in words.
         assert.strictEqual(moment(names, "-person:\"Ann Lee\" beach"), null);
         assert.strictEqual(moment(names, SearchCore.parse("-person:\"Ann Lee\" beach")), null);
         assert.strictEqual(moment(names, "smith"), null);
         assert.strictEqual(moment(names, "person 9"), null);
     });

test("the moments of a video are said as players say them", function () {
    var text = VIEWER.format.momentText;
    assert.deepStrictEqual([0, 0.4, 0.99, 12.9, 72.4, 3723.5].map(text),
                           ["0:00", "0:00", "0:00", "0:12", "1:12", "1:02:03"]);
    assert.deepStrictEqual([null, -1, "x"].map(text), ["", "", ""]);
});

test("the details page searches a person's name as the search page understood it",
     function () {
         var searched = VIEWER.format.searchedQuery;
         var rec = {i: 9, mv: 1};
         var names = [{id: 3, label: "Ada Byron", named: true, at: [8]},
                      {id: 9, label: "Person 9", named: false, at: [2]}];
         var asked = [];
         // (Plain-language search: SearchCore.interpret, where the page has it.)
         var core = {interpret: function (records, query, options) {
             asked.push([records, query, options]);
             return {query: "person:\"Ada Byron\" OR ada turtle"};
         }};
         var query = searched("ada turtle", rec, names, {typed: "", plain: true}, core);
         assert.strictEqual(query, "person:\"Ada Byron\" OR ada turtle");
         assert.strictEqual(VIEWER.format.personMoment(names, query), 8);
         assert.deepStrictEqual(asked[0][0], [rec]);
         assert.deepStrictEqual(asked[0][2].people, [{id: 3, name: "Ada Byron",
                                                      label: "Ada Byron", ids: [9],
                                                      hidden: false}]);
         assert.deepStrictEqual(asked[0][2].typed, []);
         searched("ada turtle", rec, names, {typed: "*"}, core);
         assert.strictEqual(asked[1][2].typed, "*");
         searched("ada turtle", rec, names, {typed: "p1,p2"}, core);
         assert.deepStrictEqual(asked[2][2].typed, ["p1", "p2"]);
         // Off, or no plain-language search on the page: the words as typed.
         assert.strictEqual(searched("ada turtle", rec, names, {plain: false}, core),
                            "ada turtle");
         assert.strictEqual(searched("ada turtle", rec, names, {}, null), "ada turtle");
         assert.strictEqual(searched("ada turtle", rec, names, {}, {}), "ada turtle");
         assert.strictEqual(searched("ada", rec, names, {}, {interpret: function () {
             throw new Error("no");
         }}), "ada");
         assert.strictEqual(asked.length, 3);
     });

test("the viewer and the details page start a video at the same person's moment",
     function () {
         // (With plain-language search -- SearchCore.interpret -- a person's name typed
         // as words is searched as person:; before it, there is nothing to agree on.)
         if (typeof SearchCore.interpret !== "function") {
             return;
         }
         var rec = {i: 9, mv: 1, t: "Turtle", g: ["turtle"]};
         var records = [rec, {i: 2, t: "Garden"}];
         var names = [{id: 1, label: "Ada Byron", named: true, at: [8, 12]},
                      {id: 2, label: "Bo", named: true, at: [3]}];
         var people = [{id: 1, name: "Ada Byron", label: "Ada Byron", ids: [9]},
                       {id: 2, name: "Bo", label: "Bo", ids: [9]}];
         ["ada turtle", "ada byron", "turtle"].forEach(function (typed) {
             // The search page: the query as it understood it, parsed (app.js).
             var understood = SearchCore.interpret(records, typed, {people: people});
             var viewer = VIEWER.format.personMoment(names, SearchCore.parse(understood.query));
             // The details page: the words as typed, understood again (detail.js).
             var details = VIEWER.format.personMoment(names, VIEWER.format.searchedQuery(
                 typed, rec, names, {typed: "", plain: true}, SearchCore));
             assert.strictEqual(details, viewer, typed);
         });
         assert.strictEqual(VIEWER.format.personMoment(names, VIEWER.format.searchedQuery(
             "ada turtle", rec, names, {typed: "", plain: true}, SearchCore)), 8);
     });

test("the Manage page tells the faces to look at apart", function () {
    var note = loadShell().facesNote;
    assert.strictEqual(note({faces: 7, with_faces: 4, done: 4, pictures: 7, pending: 3,
                             changed: 0, failed: 0, full: 0, videos: 0, full_pending: 0,
                             video_pending: 0, skipped: 0}, true),
                       " in 4 pictures (4 of 7 pictures looked at). 3 to look at, a job for " +
                       "the processor.");
    var text = note({faces: 154, with_faces: 30, done: 11629, pictures: 12100, pending: 471,
                     changed: 471, failed: 2, full: 1, videos: 0, full_pending: 94,
                     video_pending: 377, skipped: 565}, true);
    assert.strictEqual(text, " in 30 pictures (11,629 of 12,100 pictures looked at; 1 in full " +
                       "size). 471 to look at, a job for the processor: 94 wide or very " +
                       "large pictures in full size and 377 videos in several frames each. " +
                       "471 looked at before with other settings keep their faces until " +
                       "then. 2 could not be looked at. 565 clips of dashcam drives skipped.");
    // (Never one parenthesis in another.)
    assert.ok(!/\([^)]*\(/.test(text), text);
    assert.strictEqual(note({faces: 1, with_faces: 1, done: 1, pictures: 4, pending: 3,
                             full_pending: 1, video_pending: 1, videos: 2}, true),
                       " in 1 picture (1 of 4 pictures looked at; 2 videos in several " +
                       "frames). 3 to look at, a job for the processor: 1 wide or very large " +
                       "picture in full size, 1 video in several frames and 1 other picture.");
    // People off: what was found, nothing to look at.
    assert.strictEqual(note({faces: 1, with_faces: 1, done: 1, pictures: 4, pending: 3}, false),
                       " in 1 picture (1 of 4 pictures looked at).");
});

// The viewer as a page loads it (its Stage), with just enough of a page.
function pageViewer() {
    var noop = function () {};
    var saved = {window: global.window, document: global.document};
    global.document = {addEventListener: noop};
    global.window = {addEventListener: noop};
    var file = require.resolve(path.join(STATIC, "viewer.js"));
    delete require.cache[file];
    try {
        return require(file);
    } finally {
        delete require.cache[file];
        Object.keys(saved).forEach(function (name) {
            if (saved[name] === undefined) {
                delete global[name];
            } else {
                global[name] = saved[name];
            }
        });
    }
}

test("a video moves to a moment now, or once its player has loaded", function () {
    var viewer = pageViewer();
    var seekTo = viewer.Stage.prototype.seekTo;
    var listeners = {};
    var video = {readyState: 0, paused: true, currentTime: 0,
                 addEventListener: function (type, callback) {
                     listeners[type] = callback;
                 }};
    var stage = {rec: {i: 1, mv: 1}, video: null, drive: null};
    // Before its player is there: kept for it.
    assert.strictEqual(seekTo.call(stage, 12.5), true);
    assert.strictEqual(stage.startTime, 12.5);
    stage.video = video;
    viewer.Stage.prototype.seekWhenReady.call(stage, video);
    listeners.loadedmetadata();
    assert.strictEqual(video.currentTime, 12.5);
    assert.strictEqual(stage.startTime, null);
    // Ready: at once. A soft move (a person of the search) never moves a video
    // that was played or moved.
    video.readyState = 4;
    assert.strictEqual(seekTo.call(stage, 3), true);
    assert.strictEqual(video.currentTime, 3);
    assert.strictEqual(seekTo.call(stage, 8, true), false);
    assert.strictEqual(video.currentTime, 3);
    var fresh = {rec: {i: 2, mv: 1}, video: {readyState: 4, paused: true, currentTime: 0},
                 drive: null, moved: false};
    assert.strictEqual(seekTo.call(fresh, 8, true), true);
    assert.strictEqual(fresh.video.currentTime, 8);
    // Not a picture, nor a drive, nor a moment that is none.
    assert.strictEqual(seekTo.call({rec: {i: 3}, video: null}, 2), false);
    assert.strictEqual(seekTo.call({rec: {i: 4, mv: 1}, drive: {}, video: null}, 2), false);
    assert.strictEqual(seekTo.call(stage, NaN), false);
    assert.strictEqual(seekTo.call(stage, -1), false);
});

var SearchCore = require(path.join(STATIC, "search-core.js"));
var RECORDS = [{i: 1, t: "beach"}, {i: 2, t: "garden"}, {i: 3, t: "beach party"},
               {i: 4, t: "mountain"}];
var PEOPLE = [{id: 7, name: "Ann Lee", label: "Ann Lee", ids: [1, 3]},
              {id: 8, name: "Annika", label: "Annika", ids: [2]},
              {id: 9, name: "", label: "Person 9", ids: [3]},
              {id: 10, name: "Any", label: "Any", ids: [4]}];

function found(query, options) {
    var result = SearchCore.search(RECORDS, query, options || {people: PEOPLE});
    return {ids: result.items.slice().sort(), warnings: result.warnings};
}

test("person: finds the pictures of a person", function () {
    assert.deepStrictEqual(found("person:\"Ann Lee\"").ids, [1, 3]);
    assert.deepStrictEqual(found("person:\"ann lee\" beach").ids, [1, 3]);
    assert.deepStrictEqual(found("person:\"Person 9\"").ids, [3]);
    assert.deepStrictEqual(found("person:#8").ids, [2]);
    assert.deepStrictEqual(found("person:annik").ids, [2]);
    assert.deepStrictEqual(found("-person:\"Ann Lee\"").ids, [2, 4]);
    assert.deepStrictEqual(found("person:\"Ann Lee\" person:\"Person 9\"").ids, [3]);
    assert.deepStrictEqual(found("person:any").ids, [1, 2, 3, 4]);
    assert.deepStrictEqual(found("person:none", {people: PEOPLE.slice(0, 2)}).ids, [4]);
    // A name that starts more than one, and nobody: nothing, with a warning.
    var some = found("person:ann");
    assert.deepStrictEqual(some.ids, []);
    assert.ok(/More than one name starts with/.test(some.warnings.join(" ")), some.warnings);
    assert.ok(/nobody called "Zed"/.test(found("person:Zed").warnings.join(" ")));
});

test("person: without the people of the page", function () {
    var none = found("person:Ann", {});
    assert.deepStrictEqual(none.ids, []);
    assert.ok(/not available/.test(none.warnings.join(" ")), none.warnings);
    // (While they load, nothing is said.)
    assert.deepStrictEqual(found("person:Ann", {peoplePending: true}).warnings, []);
});

test("the person: term of a person", function () {
    assert.strictEqual(SearchCore.personTerm(PEOPLE, PEOPLE[0]), "person:\"Ann Lee\"");
    assert.strictEqual(SearchCore.personTerm(PEOPLE, PEOPLE[1]), "person:Annika");
    assert.strictEqual(SearchCore.personTerm(PEOPLE, PEOPLE[2]), "person:\"Person 9\"");
    // A name that is one of person:'s own words goes by its id.
    assert.strictEqual(SearchCore.personTerm(PEOPLE, PEOPLE[3]), "person:#10");
    var quoted = {id: 11, name: "The \"Boss\"", label: "The \"Boss\"", ids: []};
    assert.strictEqual(SearchCore.personTerm(PEOPLE.concat([quoted]), quoted), "person:#11");
    PEOPLE.forEach(function (person) {
        var term = SearchCore.personTerm(PEOPLE, person);
        assert.strictEqual(SearchCore.findPerson(PEOPLE, term.slice(7).replace(/"/g, "")),
                           person, term);
    });
});

test("hidden people: by their whole name or number, never by a part, nor as anybody",
     function () {
         var listed = PEOPLE.concat([{id: 12, name: "Annette", label: "Annette", ids: [2],
                                     hidden: true}]);
         var options = {people: listed};
         assert.deepStrictEqual(found("person:annette", options).ids, [2]);
         assert.deepStrictEqual(found("person:#12", options).ids, [2]);
         // (Annika is the only one not hidden that starts so.)
         assert.deepStrictEqual(found("person:annik", options).ids, [2]);
         assert.ok(/More than one name/.test(found("person:ann", options).warnings.join(" ")));
         var only = [PEOPLE[0], {id: 12, name: "Annette", label: "Annette", ids: [2],
                                 hidden: true}];
         assert.deepStrictEqual(found("person:anne", {people: only}).ids, []);
         assert.deepStrictEqual(found("person:any", {people: only}).ids, [1, 3]);
         assert.deepStrictEqual(found("person:none", {people: only}).ids, [2, 4]);
         assert.strictEqual(SearchCore.personTerm(only, only[1]), "person:Annette");
     });

test("SITE.peopleForSearch loads the people once, for person: only", function () {
    global.window = {location: {search: "", protocol: "http:"},
                     setTimeout: setTimeout};
    var SITE = require(path.join(STATIC, "site.js"));
    var calls = 0;
    var people = SITE.peopleForSearch(function () {
        calls += 1;
    }, true);
    assert.strictEqual(people.want("beach"), false);
    assert.strictEqual(people.want("  ", true), false);
    assert.strictEqual(people.want("beach -person:Ann"), true);
    assert.strictEqual(people.want("person:Bo"), false);         // (once)
    // (Here -- no server -- there are none: said after want returned, never inside it.)
    assert.strictEqual(calls, 0);
    var options = people.options({albums: []});
    assert.deepStrictEqual([options.people, options.peoplePending, people.pending()],
                           [null, false, false]);
    // Plain-language search: any word may be a name (words true).
    var plain = SITE.peopleForSearch(null, true);
    assert.strictEqual(plain.want("beach", true), true);
});

function failed(entry, error) {
    failures += 1;
    console.log("not ok - " + entry.name);
    console.log("    " + String(error && error.stack || error).split("\n").join("\n    "));
}

tests.forEach(function (entry) {
    try {
        entry.body();
        console.log("ok - " + entry.name);
    } catch (error) {
        failed(entry, error);
    }
});
console.log((tests.length - failures) + " of " + tests.length + " passed");
process.exitCode = failures ? 1 : 0;

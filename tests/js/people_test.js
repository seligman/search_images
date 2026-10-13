/* Tests for how the job panel names and sums up People's jobs (Find faces, Group
   faces into people, Delete face data, a scan's faces step) in static/shell.js,
   and for person: in static/search-core.js (the pictures of a person of
   /api/people), under Node (no packages needed).

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
    assert.strictEqual(people.want("beach -person:Ann"), true);
    assert.strictEqual(people.want("person:Bo"), false);         // (once)
    // (Here -- no server -- there are none: said after want returned, never inside it.)
    assert.strictEqual(calls, 0);
    var options = people.options({albums: []});
    assert.deepStrictEqual([options.people, options.peoplePending, people.pending()],
                           [null, false, false]);
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

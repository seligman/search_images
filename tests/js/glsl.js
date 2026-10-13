/* Runs functions of a GLSL shader's text under Node.js, for the tests: the
   small part of GLSL the 360 video shader's lens sums are written in
   (static/viewer.js FISHEYE_SHADER): float, vec2 and vec3 values and their
   .x .y .z, declarations, if, return, the ? : operator and the built-in
   functions in BUILT_INS. No sums of vectors: a test that runs gained()
   passes numbers for its vectors (one channel at a time).

   functions(source, names, uniforms) -> {name: a JavaScript function} for
   the functions named (they may call each other), with uniforms ({u_name:
   a number, or {x, y, z}}) and the shader's constants (const float) in
   scope. vec2(a, b) and vec3(a, b, c) make {x, y(, z)}; vec3(a) is a. */
"use strict";

var BUILT_INS = {
    acos: Math.acos,
    asin: Math.asin,
    atan: function (y, x) {
        return x === undefined ? Math.atan(y) : Math.atan2(y, x);
    },
    cos: Math.cos,
    sin: Math.sin,
    pow: Math.pow,
    clamp: function (value, low, high) {
        return Math.min(Math.max(value, low), high);
    },
    vec2: function (x, y) {
        return {x: x, y: y};
    },
    vec3: function (x, y, z) {
        return arguments.length === 1 ? x : {x: x, y: y, z: z};
    }
};

// The parameter names and the body of the function called name.
function extract(source, name) {
    var pattern = new RegExp("\\b(?:float|vec2|vec3|vec4|void)\\s+" + name +
                             "\\s*\\(([^)]*)\\)\\s*\\{");
    var match = pattern.exec(source);
    if (!match) {
        throw new Error("the shader has no function " + name);
    }
    var start = match.index + match[0].length;
    var at = start;
    var depth = 1;
    while (depth && at < source.length) {
        if (source[at] === "{") {
            depth++;
        } else if (source[at] === "}") {
            depth--;
        }
        at++;
    }
    var params = match[1].split(",").map(function (param) {
        return param.trim().split(/\s+/).pop();
    }).filter(Boolean);
    return {params: params, body: source.slice(start, at - 1)};
}

function functions(source, names, uniforms) {
    var scope = Object.assign({}, BUILT_INS, uniforms || {});
    var constant = /\bconst\s+float\s+(\w+)\s*=\s*([-+0-9.eE]+)\s*;/g;
    var found;
    while ((found = constant.exec(source)) !== null) {
        scope[found[1]] = Number(found[2]);
    }
    var made = {};
    names.forEach(function (name) {
        var part = extract(source, name);
        var body = part.body.replace(/\b(?:float|int|bool|vec2|vec3|vec4)\s+(?=\w+\s*=)/g,
                                     "var ");
        // (A function of its own text: "with" puts the scope's names in reach.)
        made[name] = new Function("scope", "with (scope) { return function (" +
                                  part.params.join(", ") + ") {" + body + "}; }")(scope);
        scope[name] = made[name];
    });
    return made;
}

module.exports = {functions: functions, extract: extract};

// Tests for speaker-name detection, with NO deps. names.js is loaded into a vm
// context exactly as the panel loads it, then held to tests/name_cases.json,
// the fixture the terminal app's pytest reads too, so the two rules cannot drift.
//
//   run:  node extension/test_names.cjs

const vm = require("vm");
const fs = require("fs");
const path = require("path");

let failures = 0;
function check(name, cond, extra) {
  if (cond) { console.log("  PASS  " + name); }
  else { console.log("  FAIL  " + name + (extra ? "  →  " + extra : "")); failures++; }
}

const ctx = { console };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname, "names.js"), "utf8"), ctx, { filename: "names.js" });

const cases = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "tests", "name_cases.json"), "utf8"));

console.log("\nnames.js: introductions and the roster, against tests/name_cases.json and roster_cases.json\n");

check("the fixture has cases of both kinds", cases.positive.length > 0 && cases.negative.length > 0);
for (const [text, want] of cases.positive) {
  const got = ctx.detectName(text);
  check(`${JSON.stringify(text)} -> ${want}`, got === want, JSON.stringify(got));
}
for (const text of cases.negative) {
  const got = ctx.detectName(text);
  check(`${JSON.stringify(text)} -> no name`, got === null, JSON.stringify(got));
}

// The cases tests/test_names.py checks beyond the fixture, so the twins agree there too.
const more = [
  ["Hi, this is Tom here. Well, my name is Thomas.", "Thomas", "cues are tried in order, not by position"],
  ["I'm Dutch, I'm Jan.", "Jan", "a rejected match does not stop the search"],
  ["I’m Sarah.", "Sarah", "a curly apostrophe works like a straight one"],
  ["I'm here.", null, "I'm is not a name"],
  ["I’m here.", null, "…with a curly apostrophe either"],
  ["Yeah, I’m Sarah’s manager.", null, "a curly possessive is still a possessive"],
  ["Okay so this is Marcus speaking for the team.", "Marcus", "this is X speaking, anywhere"],
  ["Good evening folks, this is Ana.", "Ana", "a greeting with folks"],
  ["My name is Sarah Monday.", "Sarah", "a stopped second word is dropped"],
  ["I'm Professor Lee.", "Professor Lee", "a title is kept"],
  ["I'm Doctor and engineer.", null, "a bare title is not a name"],
];
for (const [text, want, why] of more) {
  const got = ctx.detectName(text);
  check(`${why}: ${JSON.stringify(text)}`, got === want, JSON.stringify(got));
}

check("sameName: a first name matches the full name", ctx.sameName("Daniel", "Daniel Tyukov"));
check("sameName: in either order and any case", ctx.sameName("daniel tyukov", "Daniel"));
check("sameName: titles do not count", ctx.sameName("Dr. Patel", "patel"));
check("sameName: a shorter form is not the same name", !ctx.sameName("Dan", "Daniel"));
check("sameName: a different surname is a different person", !ctx.sameName("Sarah Connor", "Sarah Smith"));
check("sameName: nothing matches an empty name", !ctx.sameName("", "Sarah") && !ctx.sameName("Sarah", undefined));

// ---- the roster, against tests/roster_cases.json (tests/test_roster.py reads it too) ----
const roster = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "tests", "roster_cases.json"), "utf8"));
const show = (v) => JSON.stringify(v);
for (const [raw, want] of roster.clean) {
  const got = ctx.cleanRosterName(raw);
  check(`cleanRosterName(${show(raw)}) -> ${show(want)}`, got === want, show(got));
}
for (const [name, list, want] of roster.match) {
  const got = ctx.matchRoster(name, list);
  check(`matchRoster(${show(name)}, ${show(list)}) -> ${show(want)}`, got === want, show(got));
}
for (const [me, list, want] of roster.others) {
  const got = ctx.rosterOthers(me, list);
  check(`rosterOthers(${show(me)}, ${show(list)}) -> ${show(want)}`, show(got) === show(want), show(got));
}
for (const c of roster.eliminate) {
  const got = ctx.eliminate(c.voices, c.names, c.roster);
  check(`eliminate: ${c.why}`, show(got) === show(c.expect), show(got));
}
// tests/test_roster.py beyond the fixture
check("a two-word name needs a full match", ctx.matchRoster("Sarah Smith", ["Sarah Chen"]) === "Sarah Smith");
check("an empty name matches nothing", ctx.matchRoster("", ["Sarah Chen"]) === "");

// Inputs the panel can hand it that the fixture does not spell out.
check("undefined is not a name", ctx.detectName(undefined) === null);
check("a decomposed accent still reads as one letter",
  ctx.detectName("Hi, I'm Zoe\u0308.") === "Zo\u00eb", JSON.stringify(ctx.detectName("Hi, I'm Zoe\u0308.")));

console.log("\n" + (failures ? `${failures} FAIL` : "all passed") + "\n");
process.exit(failures ? 1 : 0);

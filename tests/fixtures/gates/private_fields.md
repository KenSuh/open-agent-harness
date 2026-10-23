# Gates: {TASK} for {NAME} in {HOME}/project

OWNS: src/**

Scope: written by {NAME} <{EMAIL}> on {WINHOME}\project

- [ ] G1: {NAME} can import {HOME}/project/data.csv
  CHECK: node {HOME}/project/scripts/check.mjs --user {EMAIL}
  OAH-INPUTS: src
  EXPECT: imported for {NAME}
  CWD: src
  EVIDENCE: pending

- [x] G2: {NAME} reviewed {WINHOME}\project\docs
  EVIDENCE: reviewed by {EMAIL} at {HOME}/project/review.txt

- [ ] G3: {NAME} signs off from {WINHOME}\Desktop
  EVIDENCE: pending

ABANDON: G3 {NAME} <{EMAIL}> is away; see {HOME}/notes and {WINHOME}\notes

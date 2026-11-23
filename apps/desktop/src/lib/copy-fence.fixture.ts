// Neutralised handover block with the same shape as a real handover prompt
// (one long briefing paragraph per line, sentence punctuation, paths, no code
// signals): exactly the input the prose heuristics used to unwrap.
export const HANDOVER_BLOCK = [
  'Übernimm den Branch feature/example-handover (PR #123, Worktree .worktrees/example, HEAD 0a1b2c3d).',
  'Log: output/session-log/example.md, Receipt: output/receipts/example.md.',
  'Nicht erneut ausführen: die Register- und Akten-Writes von heute sind fertig und validiert.',
  'Offen: Skill-Abnahme, Lane-Review der Commits, Ursache für den fehlenden Versandbeleg um 08:22.',
  'Schutz: kein Force, kein Main-Write, fremde Worktrees unangetastet.'
].join('\n')

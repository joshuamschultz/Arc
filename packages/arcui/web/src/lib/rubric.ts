// The one structured-form prompt: its body is a YAML rubric, not prose. Keyed by
// (package, name) so any agent's copy is editable via the form, not a textarea.
export const RUBRIC_PACKAGE = 'arcskill'
export const RUBRIC_NAME = 'judge_rubric'

export function isRubricPrompt(prompt: { package: string; name: string } | null): boolean {
  return prompt != null && prompt.package === RUBRIC_PACKAGE && prompt.name === RUBRIC_NAME
}

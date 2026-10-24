/** Keep provider-qualified IDs compact while preserving the full ID in titles. */
export function modelName(model: string | null | undefined): string {
  if (!model) return ''
  return model.split('/').findLast(Boolean) ?? model
}

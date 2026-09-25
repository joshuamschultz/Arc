let claimSecret = ''

/** Keep a customer claim secret only in this tab's memory. */
export function bootstrapHostedClaim(): void {
  if (window.location.pathname !== '/setup') return
  const secret = new URLSearchParams(window.location.hash.slice(1)).get('claim')
  if (!secret) return
  claimSecret = secret
  history.replaceState(null, '', window.location.pathname)
}

export function hostedClaimSecret(): string {
  return claimSecret
}

export function clearHostedClaim(): void {
  claimSecret = ''
}

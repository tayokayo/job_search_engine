# Product Specification — AI Job Search OS v0.2

## Primary loop
LinkedIn alerts → Gmail ingestion → public job verification → official company-careers verification → normalization/deduplication → CRM → scoring → company watchlist → daily digest → application strategy pack.

## Hard constraints
Normally reject roles that:
- are outside Japan/Tokyo, Singapore, Thailand/Bangkok, Taiwan/Taipei, or New York and are not meaningfully remote for one of them;
- require business/native Japanese;
- require business/native Chinese/Mandarin.

A language being preferred is not the same as required.

## Seniority
Startup/SME target: Director, Head, VP and equivalent leadership roles.
Large corporate: Principal, Staff, Senior Lead, exceptional Senior Manager, Director and above.
Judge reporting line, ownership, team scope, decision rights, geography, P&L/business ownership and strategic influence—not title alone.

## Opportunity Fit Score
Evaluate functional fit, domain fit, seniority/scope, verified evidence fit, leadership scope, career trajectory, geography, language feasibility, AI relevance and company desirability.

A = Pursue now (normally 80+)
B = Investigate (normally 60–79)
C = Ignore (normally below 60 or hard constraint)

Scores support judgment. Strategic exceptions are allowed only with an explicit rationale.

## Company Fit Score
Evaluate background fit, operating complexity, product/operations intersection, AI/transformation relevance, geography, international environment, future-role likelihood and desired-company status.

A company can enter the watchlist if it is seeded, produces an A opportunity, repeatedly produces B opportunities, or research shows unusually strong fit.

## Company intelligence
For A opportunities, retrieve recent trustworthy information about strategy, product launches, AI initiatives, expansion, leadership changes, funding or material business developments. Prefer official company sources, regulatory filings and high-quality reporting. Retain source URL and dates.

## Application Strategy Pack
For every A opportunity:
1. recommended resume base;
2. exact tailoring recommendations;
3. strongest verified candidate evidence;
4. gaps and unsupported requirements;
5. relevant current company intelligence;
6. positioning angle;
7. likely hiring manager only if confidently identifiable;
8. draft hiring-manager message grounded in the job, verified candidate facts and sourced company intelligence.

Never invent experience, relationships, referrals, metrics or company facts.

## Safety
Do not scrape authenticated LinkedIn pages, bypass access controls, automatically apply, automatically send outreach, execute unexpected downloads, or expose credentials.

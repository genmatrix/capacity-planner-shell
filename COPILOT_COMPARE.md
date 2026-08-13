# Comparing the planner against the Excel workbook with Copilot

A prompt that already knows our conventions. Without them an assistant reports
every convention difference as a defect and you spend the meeting explaining
arithmetic instead of reading the plan.

## What to give it

1. **Capacity Plan** → pick the line of business → open *"Open as a sortable
   grid"* → **Download this plan as CSV (weeks down)**. One row per week, every
   computed row, rounded to 3 decimals so a comparison is not chasing display
   rounding.
2. The Excel workbook (or the sheet for the same line of business).
3. The prompt below.

Do both for the SAME line of business and the same weeks. Comparing Member
Services against a whole-workbook total is the fastest way to a wrong answer.

## The prompt

> I have two capacity plans for the same contact-centre line of business and the
> same weeks: a CSV from our planning tool, and an Excel workbook we have used
> for years. Compare them week by week and tell me where they disagree.
>
> Align on the `Week` column. Weeks are Monday-anchored, and week 1 is the
> Monday of the week containing 1 January — check the workbook uses the same
> anchor before comparing anything, and say so if it does not.
>
> Compare these, in this order, and stop at the first one that disagrees
> materially, because everything after it inherits the difference:
> 1. Forecast contacts
> 2. Workload hours
> 3. Available hours per FTE
> 4. Required FTE
> 5. Production headcount
> 6. Staffed FTE
> 7. Net FTE
>
> Report each as: week, the two values, the difference, and the percentage.
> Flag anything over 2% or over 5 FTE. Give me the three largest gaps and, for
> each, which of the seven steps above it first appears at.
>
> **These differences are EXPECTED. Do not report them as errors — report them
> separately, quantified, as "known convention differences":**
>
> - **Seasonality is normalised in the tool and probably not in the workbook.**
>   The tool scales the weekly index so the ANNUAL total is preserved: a uniform
>   1.2 changes nothing, only relative shape moves volume between weeks. If the
>   workbook multiplies raw, its annual total will be higher by roughly the mean
>   index. Compare ANNUAL totals first; if those match and individual weeks do
>   not, this is why.
> - **Contacts per member is an annual MULTIPLIER**, never a divisor:
>   `weekly contacts = members × CPM ÷ 52 × seasonality`. If the workbook
>   divides, its forecast will be wrong by orders of magnitude — that IS worth
>   flagging loudly.
> - **Shrinkage may be trended per week in the tool and flat in the workbook.**
>   It sits in the denominator (`paid × (1 − shrink) × occupancy`), so a flat
>   average is within ~0.1% on the annual figure but can be 10+ FTE out in
>   individual weeks. Tell me the weeks where the two shrinkage assumptions
>   differ, before comparing Required FTE.
> - **Requirement basis.** The tool can size with workload maths or Erlang C.
>   Erlang carries a real service-level premium: roughly 1.5× workload on a very
>   small queue, converging to about 1.01× on a large one. If the workbook uses
>   workload maths, compare against the tool's `Workload Req FTE` column, not
>   `Required FTE`, and say which you used.
> - **Occupancy is a CAP under Erlang, not a divisor.** Under workload maths it
>   IS a divisor. Do not assume one rule for both.
> - **Mentors** — agents pulled off the phones to coach a new-hire class — come
>   out of Staffed FTE but stay in Production headcount. If the workbook has no
>   such row, the tool will show lower Staffed FTE for those weeks and that is
>   correct, not a defect.
> - **Part-time agents never attrite in the tool.** Full-time and part-time are
>   separate typed counts; the attrition walk (modelled rate AND recorded
>   departures) runs on the full-time count only, and each part-timer counts as
>   part-time hours ÷ paid hours of an FTE in Staffed. If the workbook attrites
>   everyone or splits by a percentage, its headcount will drift slightly below
>   the tool's over the year on any line with part-timers — expected, not a
>   defect. Compare the full-time walks against each other first.
> - **Attrition** may be a modelled rate in one and recorded actuals in the
>   other. The tool treats a blank in a fully elapsed week as zero departures,
>   so past weeks are a ledger rather than a forecast. Compare the rates
>   themselves, and tell me the annualised rate each implies.
>
> Finally: list anything that disagrees and is NOT explained by the list above.
> That is the only part I need to act on. If the two plans agree everywhere
> except the known conventions, say so plainly rather than manufacturing
> findings.

## Reading the answer

The order matters. A difference at step 1 (forecast) makes every later step
differ, so chasing a Net FTE gap before the forecast matches wastes the
comparison. That is why the prompt asks where a gap FIRST appears.

If it reports a difference the list does not cover, that is the useful output —
bring it back and we will work out which model is right.

## What this will not settle

An assistant can tell you the two plans differ and by how much. It cannot tell
you which is correct, because that depends on decisions the team has to make —
what shrinkage to plan on, whether to plan attrition at the measured rate or a
buffered one, whether to size on Erlang or workload. Those are the conversations
the comparison is FOR.

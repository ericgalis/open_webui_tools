# Open WebUI Automations (User menu > Automations)

Each person creates these under their own account, so the Health Data tool reads their own database.
Times assume the 03:30 ingest has finished. Use "Run now" once after creating each one.

## Daily brief: every day at 06:30, model "Health Coach"

```
Daily health brief. Call health_report with period "day" (end = yesterday). Then write, in this order and under 250 words:

1. Yesterday in one line: sleep (hours, clock bedtime and wake time), resting HR, HRV, steps, training, food logged or not.
2. What stood out: only metrics from notable_changes, each with its value, baseline and direction. If nothing is notable, say "Nothing unusual."
3. Data gaps or stale sources, if any.
4. One suggestion for today, tied to a number above (e.g. an easier session if readiness is low and HRV is below baseline).

No other advice. If a clinician-referral condition from your instructions applies, add it at the end.
```

## Weekly review: Sundays at 07:00, model "Health Coach"

```
Weekly health review. Call health_report with period "week" (end = yesterday), health_activities for the same 7 days with include_sets true, and health_food for the same 7 days with include_items false. If useful, call health_trend for up to two metrics over the last 12 weeks.

Write:
1. The week in numbers: a short table of the key metrics (sleep, bedtime consistency, resting HR, HRV, steps, intensity minutes, strength sessions and volume, weight and body fat if measured, average kcal and protein on logged days) with the baseline and the change.
2. Targets: met or missed, from the targets section.
3. What changed and the most likely reasons, citing the numbers. Note data gaps.
4. Recommendations for next week, at most three, one per area where relevant:
   - Habits (sleep timing, recovery, stress)
   - Diet (protein, fiber, energy balance, meal timing)
   - Exercise routine (volume, intensity, progression for the strength lifts, cardio)
   Each one concrete, measurable, and tied to a number above.
5. One thing to check next week.
```

## Monthly deep review: the 1st of each month at 07:30, model "Health Coach (deep)"

```
Monthly health review of the previous calendar month. Call health_report with period "month" (end = yesterday), health_profile, health_trend for weight_kg, body_fat_pct, resting_hr, hrv_ms, sleep_h and strength_volume_kg over the last 6 months by week, health_compare for the same metrics between the month before last and last month, and health_correlate for at most three relationships you think matter (e.g. bedtime_h vs sleep_score, protein_g_per_kg vs muscle_mass_kg with lag 7, active_kcal vs weight_kg).

Write a structured review:
1. Summary in three sentences.
2. Progress toward each goal in the profile, with the trend per week and the projected date to reach any numeric target at the current rate (only if the tool's trend_r2 is at least 0.3).
3. What is working, what is not, with the numbers.
4. Relationships found (correlations with their strength and number of pairs; say clearly when the evidence is weak).
5. The plan for next month: up to five changes across habits, diet and the exercise routine, each specific, measurable, and with the reason. Say which of last month's recommendations to keep, drop or adjust if the data shows their effect.
6. Data quality: gaps, stale sources, unresolved food items.

You are not a doctor; follow the clinician-referral rules in your instructions.
```

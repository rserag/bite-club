# Packaged foods and reviewed labels

Use `/label` to add a packaged food from readable nutrition-label text. You can
also choose **Enter food label** while searching for a missing food; the pending
meal or recipe remains available after the food is saved. No server command or
JSON file is needed.

Enter the product name, including its brand or variant, and choose whether the
label describes it **As sold** or **As prepared**. Choose **Per 100 g** or
**Per serving**. For a serving label, enter its printed edible mass in grams.
Counts, cups and millilitres do not establish a mass unless the label supplies one.
The amount eaten is a separate question after food composition is saved.

Type or paste the reported values as lines or separated by semicolons. For
example, the following values are synthetic and are not a food reference:

```text
Energy 120 kcal; Protein 10 g; Fat 4 g
Total carbohydrate including fiber 12 g; Fiber 2 g; Sodium 100 mg
```

The guide supports energy, protein, total carbohydrate including fiber, fat,
fiber, sodium, potassium, calcium, magnesium, iron, zinc and vitamins C, D and
B12. Enter kcal for energy and the label's g, mg or ug for nutrient mass. `µg`,
`μg` and `mcg` are accepted as explicit microgram units. An omitted nutrient or
`Calcium unknown` remains unknown; an explicit reported `Protein 0 g` stays a
known zero. At least one supported, explicitly reported value is required.
Sugars and saturated fat are disclosed as outside the current nutrient registry.

If you enter `carbs` or another ambiguous carbohydrate name, the guide asks whether
it includes fiber. Confirm that definition from the label. Available carbohydrate
excluding fiber, or an unknown definition, cannot become total carbohydrate.
The guide does not add fiber to an available-carbohydrate value. Salt is not
sodium. Vitamin D in IU and energy in kJ remain unknown in their registered mass
and kcal fields unless the label supplies compatible values separately. Percent
Daily Values do not establish nutrient masses.

Review the product, preparation, serving basis, reported values and missing
nutrients, then tap **Save food** on the current preview. This saves the label
composition only. **Log this food** starts a meal quantity; **Continue meal**
resumes the pending meal or ingredient selection. Measured meals use deterministic
scaling from the label's serving basis. Rough portions still need fresh Telegram
approval of the displayed draft revision.

**Correct label** starts an explicitly selected correction and appends an immutable
food version. Prior meals retain their original label values. Changed or expired
step buttons cannot save an old preview, and a repeated Save cannot create another
food. Guided steps survive restart and expire after 30 minutes without activity.
Cancel abandons the guide without recording consumption.

This manual flow does not extract nutrient values from a photo. If you have a label
photo, type or paste its readable values and keep the printed label beside the
preview while reviewing. Photo interpretation requires a separately configured,
evaluated route; the manual flow does not activate it or purchase model access.

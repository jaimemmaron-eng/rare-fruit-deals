# Rare Fruit Deals

Price comparison for rare named fruit plants and perennial vegetables: every seller, with the delivered price.

- `index.html` is the whole site (styles and script inline).
- `data/` holds one JSON file per category plus shipping rules. `data/fruits.json` is the category list.
- `seeds/seeds.js` is the free seed request page, loaded on demand.
- `tools/` holds the scripts the scheduled price checks use to validate and apply changes. Never edit data files by hand.

The site is static and is served by GitHub Pages from the `main` branch.

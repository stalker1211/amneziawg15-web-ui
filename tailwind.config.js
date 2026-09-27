// Tailwind build config for build_css.sh (standalone CLI, no Node project).
//
// Dark mode keys off a `dark` class on <body>, set by app.js from the
// System/Light/Dark preference. The variant matches that element itself as well as
// its descendants, and :is() keeps the specificity of a class, so a dark: class
// beats the plain utility it pairs with (e.g. `bg-white dark:bg-[#1f2937]`) and
// wins over hover: variants on the same property, as the old overrides did.
module.exports = {
    content: ["./web-ui/templates/**/*.html", "./web-ui/static/js/**/*.js"],
    darkMode: ["variant", "&:is(.dark, .dark *)"],
    theme: { extend: {} },
    plugins: [],
};

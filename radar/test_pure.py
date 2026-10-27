#!/usr/bin/env python3
"""
test_pure.py - unit tests for the pure functions, as a refactoring safety net.

    python3 test_pure.py            # or: python3 -m unittest test_pure -v

selftest.py proves the lanes RUN. This proves they still DECIDE the same way,
which is a different question and the one that matters when moving code. A
sweep that completes happily while classify_location has started reading
"Americana, Los Angeles" as American is a worse failure than a crash, because
nothing reports it.

Every expectation here was produced by RUNNING the function and reading the
result, never by reasoning about what it ought to return. That is not
pedantry: two expectations written the other way round on 2026-10-27 were
wrong, and "Remote (United States / Canada)" returns CA rather than CA-OPTION,
which is not what you would guess from the name.

Most cases are regressions for bugs the comments in the source record as
having actually happened. They are the highest-value tests in the file,
because each one has already cost somebody a day.

Standard library only - unittest, no pytest.
"""
import glob
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import adapters                                                    # noqa: E402
import fetchcore as fc                                             # noqa: E402
import job_radar as jr                                             # noqa: E402

# title_match consults VENDORS, which main() normally fills from targets.json.
# Without this the vendor family never matches and a third of the title rules
# are silently untested.
jr.VENDORS.update(x.lower() for x in
                  json.load(open(jr.TARGETS)).get("devops_vendors", []))


def _mod(name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, path))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestClassifyLocation(unittest.TestCase):
    """Geography is the filter that decides what you never see, so a wrong
    bucket is invisible by construction - the posting simply is not there."""

    CASES = [
        # (location, bucket, kept)
        ("Toronto, ON",                        "CA",        True),
        ("Mississauga, Ontario, Canada",       "CA",        True),
        ("Hamilton, ON",                       "CA",        True),
        ("London, ON",                         "CA",        True),
        ("Remote, Canada",                     "CA",        True),
        # Both halves of a dual posting count as reachable.
        ("Remote (United States / Canada)",    "CA",        True),
        ("2 Locations",                        "MULTI",     True),
        ("Worldwide",                          "GLOBAL",    True),
        ("Home based - Worldwide",             "GLOBAL",    True),
        # The bare-Americas rule, added 2026-10-27: 74 postings were being
        # dropped as OTHER.
        ("Home Based - Americas",              "NA-REMOTE", True),
        ("Americas",                           "NA-REMOTE", True),
        ("North & South America",              "NA-REMOTE", True),
        # ...and the veto that keeps LATAM out of it.
        ("Latin America - Remote",             "OTHER",     False),
        ("LATAM - Remote",                     "OTHER",     False),
        # "Americana" is a Brazilian city AND a Los Angeles mall, and both
        # appear in real location fields. Matching "america" as a substring
        # claimed them; the rule is plural-only for exactly this reason.
        ("Americana, Los Angeles",             "OTHER",     False),
        ("Americana, Glendale",                "OTHER",     False),
        # Ambiguous city names need a province or "canada" beside them.
        # "Burlington, MA" and "Melbourne, Victoria" were both read as
        # Canadian before CA_CITIES_AMBIGUOUS existed.
        ("Burlington, MA",                     "OTHER",     False),
        ("Melbourne, Victoria, Australia",     "OTHER",     False),
        ("Hamilton, Bermuda",                  "OTHER",     False),
        ("London, UK",                         "OTHER",     False),
        ("Remote - US",                        "US-ONLY",   False),
        ("United States of America",           "US-ONLY",   False),
        ("Bangalore, India",                   "OTHER",     False),
        ("Dublin, Ireland",                    "OTHER",     False),
        # A bare "Remote" is NOT global. Airbnb's own postings say
        # "US - Remote Eligible ... a state where Airbnb has an entity".
        ("Remote",                             "OTHER",     False),
        ("",                                   "OTHER",     False),
    ]

    def test_buckets(self):
        for loc, bucket, keep in self.CASES:
            with self.subTest(loc=loc):
                self.assertEqual(jr.classify_location(loc), (bucket, keep))

    def test_substring_matching_never_returns(self):
        """'amer' inside 'United States of America' and 'milton' inside
        'Hamilton' each caused a wrong bucket once. Word boundaries only."""
        self.assertEqual(jr.classify_location("Hamilton, Bermuda")[0], "OTHER")
        self.assertNotEqual(jr.classify_location("Milton, ON")[0], "OTHER")


class TestTitleMatch(unittest.TestCase):
    """A tier-0 title is invisible, not merely ranked low - it never reaches
    score() at all. Recall bugs here cost whole categories of job."""

    MATCH = [
        ("DevOps Engineer", 4), ("Site Reliability Engineer", 4),
        ("Cloud Engineer", 4), ("Senior DevOps Engineer", 4),
        # Added 2026-10-27; all were tier 0 and therefore invisible.
        ("DevSecOps Engineer", 4),
        ("Dev Ops II, Public Cloud", 4),
        ("Kubernetes (K8S) / HPC Engineer", 4),
        ("Platform Administration Lead with Management Experience", 4),
        ("VMware Cloud Foundation (VCF) Operations Engineer", 3),
    ]
    # Must stay out. The first is the whole reason the platform rule carries a
    # (?<!power ) lookbehind; it arrived in the same week as the ones above.
    NO_MATCH = ["Power BI & Power Platform Administrator",
                "Specialist Systems, IT", "Second Cook",
                "Structural Engineer-in-Training"]

    def test_matches(self):
        for title, tier in self.MATCH:
            with self.subTest(title=title):
                self.assertEqual(jr.title_match(title, "x")[0], tier)

    def test_non_matches(self):
        for title in self.NO_MATCH:
            with self.subTest(title=title):
                self.assertEqual(jr.title_match(title, "x"), (0, None))

    def test_adding_a_rule_can_only_raise_a_tier(self):
        """title_match returns the BEST tier among all matching rules, which
        is what makes new rules safe to add: nothing can lose its tier."""
        self.assertGreaterEqual(jr.title_match("DevOps Engineer", "x")[0],
                                jr.title_match("Cloud Operations", "x")[0])


class TestLevelOf(unittest.TestCase):
    CASES = [("DevOps Engineer", "mid"), ("Senior DevOps Engineer", "senior"),
             ("Sr. DevOps Engineer", "senior"), ("DevOps Engineer III", "senior"),
             ("Junior DevOps Engineer", "entry"), ("DevOps Intern", "intern"),
             ("Director of Engineering", "exec"), ("DevSecOps, Lead", "exec"),
             # "Senior Associate" is a grade BELOW senior at the consultancies
             # that use it, so it is deliberately read as mid.
             ("Senior Associate Engineer", "mid")]

    def test_levels(self):
        for title, lvl in self.CASES:
            with self.subTest(title=title):
                self.assertEqual(jr.level_of(title), lvl)


class TestScore(unittest.TestCase):
    def test_aliases_score_once(self):
        """'Kubernetes (K8s)' used to score the one skill twice. 45 of 177
        rows were inflated by 1-2 points before groups were deduped."""
        many = jr.score("DevOps Engineer", "x", 4, "devops",
                        "kubernetes k8s kubectl eks aks openshift")[0]
        one = jr.score("DevOps Engineer", "x", 4, "devops", "kubernetes")[0]
        self.assertEqual(many, one)

    def test_canonical_name_is_reported_not_the_synonym(self):
        _s, kw, _l = jr.score("DevOps Engineer", "x", 4, "devops", "k8s and terragrunt")
        self.assertIn("kubernetes", kw)
        self.assertIn("terraform", kw)

    def test_level_moves_the_score_in_the_right_direction(self):
        blob = "kubernetes terraform aws docker"
        entry = jr.score("Junior DevOps Engineer", "x", 4, "devops", blob)[0]
        mid = jr.score("DevOps Engineer", "x", 4, "devops", blob)[0]
        senior = jr.score("Senior DevOps Engineer", "x", 4, "devops", blob)[0]
        execu = jr.score("Director of DevOps", "x", 4, "devops", blob)[0]
        self.assertGreater(entry, mid)
        self.assertGreater(mid, senior)
        self.assertGreater(senior, execu)

    def test_empty_blob_does_not_crash(self):
        for blob in ("", None):
            with self.subTest(blob=blob):
                s, kw, _ = jr.score("DevOps Engineer", "x", 4, "devops", blob)
                self.assertEqual(kw, [])
                self.assertIsInstance(s, int)

    def test_kw_list_is_capped_and_ordered_by_weight(self):
        blob = " ".join(f[0] for f in jr.SKILL_GROUPS)
        _s, kw, _l = jr.score("DevOps Engineer", "x", 4, "devops", blob)
        self.assertLessEqual(len(kw), 10)
        weights = {c: w for c, w, _ in jr.SKILL_GROUPS}
        self.assertEqual(weights[kw[0]], max(weights[k] for k in kw))


class TestDiscoverHelpers(unittest.TestCase):
    dc = None

    @classmethod
    def setUpClass(cls):
        cls.dc = _mod("dc", "discover.py")

    def test_legal_suffix_is_stripped(self):
        """LinkedIn renders the employer as registered - 'Tecsys Inc.' - so
        every variant was tecsysinc and the real board, tecsys, was unreachable."""
        self.assertIn("tecsys", self.dc.variants("Tecsys Inc."))
        self.assertIn("interac", self.dc.variants("Interac Corp."))

    def test_descriptive_words_are_NOT_stripped(self):
        """Taking 'Solutions' off 'Enterprise Solutions Inc' would probe
        'enterprise' - a live Recruitee DEMO account that answers 200 with
        sample postings. Five boards were added that way once."""
        self.assertNotIn("enterprise", self.dc.variants("Enterprise Solutions Inc"))

    def test_tier2_is_a_superset_of_tier1(self):
        for n in ("Foo Inc", "MaintainX", "Smile Digital Health"):
            with self.subTest(n=n):
                self.assertTrue(set(self.dc.variants(n))
                                <= set(self.dc.variants(n, 2)))

    def test_title_overlap_matches_by_containment(self):
        mt, _bt = self.dc.title_overlap(
            ["Cloud Engineer"], ["Cloud Engineer, Automation (Remote Ontario)"])
        self.assertEqual(mt, "Cloud Engineer")

    def test_title_overlap_rejects_short_strings(self):
        """An 8-character floor, or 'SRE' matches half the internet."""
        self.assertEqual(self.dc.title_overlap(["SRE"], ["Site Reliability Engineer"]),
                         (None, None))

    def test_title_overlap_rejects_the_known_impostor(self):
        """greenhouse/nasdaq answers 200 with an AVP and a sysadmin, and
        nothing resembling the DevOps Cloud Engineer that was mailed."""
        self.assertEqual(
            self.dc.title_overlap(["DevOps Cloud Engineer"],
                                  ["AVP - Product Strategy",
                                   "Specialist - Systems and Network Administration"]),
            (None, None))


class TestInboxParsing(unittest.TestCase):
    ib = None

    @classmethod
    def setUpClass(cls):
        cls.ib = _mod("ib", "inbox.py")

    def test_alt_company_skips_chrome(self):
        frag = ('<img alt="LinkedIn"><img alt="Messaging icon">'
                '<img alt="ElevenLabs">')
        self.assertEqual(self.ib._alt_company(frag), "ElevenLabs")

    def test_alt_company_unescapes(self):
        self.assertEqual(self.ib._alt_company('<img alt="K&amp;K Global">'),
                         "K&K Global")

    def test_title_anchor_must_point_at_this_job(self):
        """The account footer falls inside the last segment, and every one of
        its links is a plausible title. Blocklisting them by name fixed six
        rows and mis-titled eight more as 'Help'; matching on the href cannot
        be outrun that way."""
        frag = ('<a href="https://www.linkedin.com/jobs/view/123">Infrastructure Engineer</a>'
                '<a href="https://www.linkedin.com/help">Help</a>'
                '<a href="https://www.linkedin.com/settings">Manage recommendations</a>')
        self.assertEqual(self.ib._title_from(frag, "123"), "Infrastructure Engineer")

    def test_li_segments_groups_by_job_id(self):
        # Absolute URLs: LI_VIEW anchors on the linkedin.com host, so a
        # relative href is not a posting link. (This test failed first time
        # because the FIXTURE was wrong, not the parser - worth keeping the
        # real shape rather than a convenient one.)
        base = "https://www.linkedin.com"
        html = (f'<a href="{base}/jobs/view/111"></a>'
                f'<a href="{base}/jobs/view/111">A</a>'
                f'<a href="{base}/comm/jobs/view/222"></a>'
                f'<a href="{base}/comm/jobs/view/222">B</a>')
        segs = self.ib.li_segments(html)
        self.assertEqual([s[0] for s in segs], ["111", "222"])

    def test_clean_loc_prefers_a_province_code(self):
        """A province code is an unambiguous end-of-location marker; a list of
        badge names never will be, because the next redesign adds one more."""
        self.assertEqual(
            self.ib._clean_loc("Toronto, ON Actively recruiting 13 school alumni"),
            "Toronto, ON")

    def test_employer_skiplist(self):
        for name in ("LinkedIn", "Jobgether", "Indeed"):
            with self.subTest(name=name):
                self.assertTrue(self.ib.EMP_SKIP.match(name))
        self.assertFalse(self.ib.EMP_SKIP.match("Shopify"))


class TestRatecheck(unittest.TestCase):
    rc = None

    @classmethod
    def setUpClass(cls):
        cls.rc = _mod("rc", "ratecheck.py")

    def test_http_429_pattern_ignores_digits_in_urls(self):
        """`"429" in line` counted 6 non-errors out of 714 - sweep summaries
        whose scanned count contained those digits, and two job URLs."""
        self.assertTrue(self.rc.HTTP_429.search(
            "- workday/TC Energy: HTTPError HTTP Error 429: Too Many Requests"))
        self.assertFalse(self.rc.HTTP_429.search(
            "- https://jobs.canadalife.com/job/Toronto-x/1451429733/"))
        self.assertFalse(self.rc.HTTP_429.search(
            "_0 shown · 143 matched · 56429 scanned · 1126 boards"))

    def test_board_name_may_contain_spaces(self):
        """`- (\\S+):` could not match 'workday/TC Energy:', so the worst
        offender - 116 errors - never appeared in the report at all."""
        m = self.rc.BOARD_RE.match(
            "  - workday/TC Energy: HTTPError HTTP Error 429: Too Many Requests")
        self.assertEqual(m.group(1).strip(), "workday/TC Energy")


class TestFetchcore(unittest.TestCase):
    """The transport layer's pure half - the HTML and date helpers.

    strip_html feeds the scorer, so a change here moves every score in the
    tracker. It had no tests until the module boundaries were drawn, which is
    also when drop_scripts() was pulled out of it.
    """

    def test_script_bodies_are_not_prose(self):
        """A Global Relay posting arrived as 25,588 characters of Google Tag
        Manager config in which 'Kubernetes' appeared only inside a JS string.
        The tag stripper alone turns that into keywords the scorer believes."""
        self.assertEqual(
            fc.strip_html('<div>Role</div><script>var kw="Kubernetes";'
                          '</script><p>Terraform</p>'),
            " Role Terraform ")

    def test_numeric_entities_are_folded_to_ascii(self):
        """WordPress REST titles came through as 'Business Analyst &#8211;
        Senior' - visible in the tracker and in push notifications."""
        self.assertEqual(fc.strip_html("Business Analyst &#8211; Senior"),
                         "Business Analyst - Senior")

    def test_named_entities_are_unescaped(self):
        self.assertEqual(fc.strip_html("<b>R&amp;D</b>&nbsp;Lead"), " R&D Lead")

    def test_entities_outside_the_fold_table_survive_as_unicode(self):
        """&#8230; is folded to '...' by hand, but &hellip; is not in the
        table and html.unescape gives the real character. Asserted because it
        is asymmetric, not because it is ideal."""
        self.assertEqual(fc.strip_html("Dev&#x27;s &hellip; team"),
                         "Dev's \u2026 team")

    def test_empty_input_never_raises(self):
        for x in ("", None):
            with self.subTest(x=x):
                self.assertEqual(fc.strip_html(x), "")
                self.assertEqual(fc.drop_scripts(x), "")

    def test_drop_scripts_keeps_the_markup(self):
        """verify_tracked searches a page for 'no longer accepting
        applications'. It needs the script bodies gone and the tags left, so
        this is not strip_html."""
        self.assertEqual(fc.drop_scripts("<script>x</script><!--c--><b>keep</b>"),
                         "  <b>keep</b>")

    def test_drop_scripts_removes_scripts_before_comments(self):
        """The order is observable on malformed HTML: a comment that opens
        before a <script> and closes inside its body. Script-first is the
        choice, because strip_html - which feeds the scorer and the golden
        master - has always done it that way. Measured over 60 real tracked
        pages, the two orders produced identical output on all of them."""
        self.assertEqual(fc.drop_scripts("a <!-- <script> --> b </script> c"),
                         "a <!--   c")

    def test_TAG_is_public_because_it_strips_without_a_space(self):
        """SuccessFactors renders a title across adjacent spans, so inserting
        a space the way strip_html does would split words."""
        html = "<span>Cloud</span><span>Engineer</span>"
        self.assertEqual(fc.TAG.sub("", html), "CloudEngineer")
        self.assertEqual(fc.strip_html(html), " Cloud Engineer ")

    def test_long_date_is_locale_independent(self):
        """cron runs with a minimal environment, so strptime('%B') would make
        the date silently None on exactly the runs that matter. Amazon also
        space-pads to a fixed width."""
        self.assertEqual(fc.long_date("September  1, 2026"), "2026-09-01")
        self.assertEqual(fc.long_date("October 23, 2026"), "2026-10-23")

    def test_long_date_returns_empty_not_none(self):
        """days_ago() is called on the result, and '' is what it tolerates."""
        for x in ("not a date", "Smarch 3, 2026", "", None):
            with self.subTest(x=x):
                self.assertEqual(fc.long_date(x), "")

    def test_days_ago_tolerates_junk(self):
        for x in (None, "", "x"):
            with self.subTest(x=x):
                self.assertIsNone(fc.days_ago(x))

    def test_job_is_the_one_record_shape(self):
        """Every adapter returns a list of these, and the whole pipeline
        indexes them by these keys."""
        j = fc.job("src", "co", "title", None, "url")
        self.assertEqual(sorted(j), ["age", "blob", "company", "hyd",
                                     "location", "offices", "posted",
                                     "source", "title", "updated", "url"])
        self.assertEqual(j["location"], "")
        self.assertEqual(j["posted"], "")
        self.assertEqual(j["updated"], "")
        self.assertEqual(j["offices"], [])
        self.assertIsNone(j["age"])


class TestAdaptersContract(unittest.TestCase):
    """The package's own surface - no network.

    An adapter that is importable but not wired into the registry loses a
    whole platform in silence, which is the failure these assert against.
    """

    def test_every_adapter_has_its_fetch_function(self):
        for name, fn in adapters.ADAPTERS.items():
            with self.subTest(name=name):
                self.assertIs(fn, getattr(adapters.platform(name),
                                          "fetch_" + name))

    def test_registry_matches_the_modules_on_disk(self):
        """A module that is importable but not wired in loses a whole platform
        in silence. The package __init__ refuses to import in that case; this
        asserts the same thing from the outside, off the filesystem."""
        on_disk = {os.path.basename(f)[:-3]
                   for f in glob.glob(os.path.join(HERE, "adapters", "*.py"))
                   if not os.path.basename(f).startswith("_")}
        self.assertEqual(set(adapters.ADAPTERS), on_disk)
        self.assertEqual(len(adapters.ADAPTERS), 21)

    def test_hydrators_are_a_subset_and_callable(self):
        self.assertLessEqual(set(adapters.HYDRATORS), set(adapters.ADAPTERS))
        for name, fn in adapters.HYDRATORS.items():
            with self.subTest(name=name):
                self.assertTrue(callable(fn))

    def test_expand_is_identity_without_a_fan_out(self):
        """Most platforms have an 'all jobs' view, so the target is the task."""
        self.assertEqual(adapters.expand("greenhouse", {"t": 1}), [{"t": 1}])
        self.assertEqual(adapters.expand("not-a-platform", {"t": 1}), [{"t": 1}])

    def test_expand_fans_workday_out_per_keyword(self):
        """Workday has no 'all IT jobs' view, so the board is swept once per
        search term."""
        out = adapters.expand("workday", {"t": 1})
        self.assertEqual(len(out), 13)
        self.assertEqual({q["_query"] for q in out}, set(adapters.platform(
            "workday").WD_QUERIES))

    def test_expand_reads_taleo_queries_off_the_target(self):
        """Taleo facets are per-employer, so the queries live in the target.
        The default is category 177, Information Technology."""
        self.assertEqual(adapters.expand("taleo", {"base": "b"}),
                         [{"base": "b", "_query": ("category", "177")}])
        got = adapters.expand("taleo", {"base": "b",
                                        "queries": [["keywords", "sre"]]})
        self.assertEqual(got[0]["_query"], ("keywords", "sre"))

    def test_platform_resolves_a_name_to_its_module(self):
        self.assertEqual(adapters.platform("ashby").__name__, "adapters.ashby")
        self.assertIsNone(adapters.platform("not-a-platform"))

    def test_successfactors_identifies_both_result_shapes(self):
        """hunt.py used to test SF_ROW and SF_TILE itself to decide whether a
        careers page was an RMK site. RMK renders results as either."""
        sf = adapters.platform("successfactors")
        self.assertTrue(sf.looks_like('<table><tr class="data-row">x</tr></table>'))
        self.assertTrue(sf.looks_like('<ul><li class="job-tile foo">x</li></ul>'))
        for x in ("<p>careers</p>", "", None):
            with self.subTest(x=x):
                self.assertFalse(sf.looks_like(x))


class TestGoldenMaster(unittest.TestCase):
    """259 real postings, with the verdict the code gives today.

    The hand-written cases above cover what someone thought to write down.
    This covers what nobody did: it is stratified over every (bucket, tier)
    combination present in a 56,524-posting snapshot, so each path through
    classify_location and title_match is exercised by real input rather than
    invented input.

    Its purpose is drift detection during a refactor, not correctness - it
    asserts only that behaviour has not CHANGED. If a change here is
    deliberate, regenerate the file and the diff becomes the record of
    exactly which postings the change moved, which is the review you want.
    """

    GOLDEN = os.path.join(HERE, "testdata_golden.jsonl")

    def test_no_drift(self):
        self.assertTrue(os.path.exists(self.GOLDEN),
                        "testdata_golden.jsonl is missing - regenerate it")
        drift = []
        n = 0
        with open(self.GOLDEN) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                n += 1
                c = json.loads(line)
                tier, fam = jr.title_match(c["title"], c["company"])
                bucket, keep = jr.classify_location(c["location"])
                got = (tier, fam, bucket, keep, jr.level_of(c["title"]))
                want = (c["tier"], c["family"], c["bucket"], c["keep"],
                        c["level"])
                if got != want:
                    drift.append(
                        f"\n  {c['title'][:48]!r} @ {c['location'][:28]!r}"
                        f"\n    was {want}\n    now {got}")
        self.assertEqual(n, 259, "golden file changed size unexpectedly")
        self.assertFalse(drift,
                         f"{len(drift)} of {n} cases changed verdict:"
                         + "".join(drift[:10]))


if __name__ == "__main__":
    unittest.main(verbosity=2)

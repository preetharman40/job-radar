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
import re
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


class TestInboxDedupe(unittest.TestCase):
    """The alert-mail dedupe. No network, no IMAP.

    Every one of these is a real row from the 2026-10-27 run that either
    buzzed the phone when it should not have, or would have been lost if the
    noise filtering had been done by removing rows instead of labelling them.
    """

    ib = None

    @classmethod
    def setUpClass(cls):
        cls.ib = _mod("ib2", "inbox.py")

    # ---- company matching -------------------------------------------------
    def _same(self, a, b):
        return self.ib._same_employer(self.ib._co_tokens(a),
                                      self.ib._co_tokens(b))

    def test_the_mail_and_the_board_spell_an_employer_differently(self):
        """`seen` held "Associate, DevOps Engineer" at BMO; the mail said BMO
        Capital Markets. Exact normalising made that a near-miss, so the one
        real duplicate of the night went to the phone."""
        self.assertTrue(self._same("BMO Capital Markets", "BMO"))
        self.assertTrue(self._same("The Nationwide Group", "Nationwide"))
        self.assertTrue(self._same("Tecsys Inc.", "Tecsys"))

    def test_a_shared_word_is_not_a_shared_employer(self):
        """Canada Life is not Sun Life. An earlier draft stripped "canada" as
        a generic word and merged the two insurers."""
        self.assertFalse(self._same("Canada Life", "Sun Life"))
        self.assertFalse(self._same("Telus Health", "telus-digital"))
        self.assertFalse(self._same("Keyfactor", "Scotiabank"))

    def test_substring_matching_is_not_used(self):
        """"interac" sits inside "guesttekinteractiveentertainment", so raw
        containment would have dropped a real GuestTek posting as a duplicate
        of an Interac one."""
        self.assertFalse(
            self._same("GuestTek Interactive Entertainment Ltd", "Interac"))

    def test_an_unnameable_company_falls_back_to_equality(self):
        for a, b, want in (("", "", True), ("", "BMO", False),
                           ("Ltd", "Inc", False)):
            with self.subTest(a=a, b=b):
                self.assertEqual(self._same(a, b), want)

    # ---- url normalising --------------------------------------------------
    def test_url_compares_the_way_the_tracker_stores_it(self):
        for raw, want in (
                ("https://www.linkedin.com/jobs/view/4477455763",
                 "https://www.linkedin.com/jobs/view/4477455763"),
                ("https://www.linkedin.com/jobs/view/4477455763?refId=x",
                 "https://www.linkedin.com/jobs/view/4477455763"),
                ("https://www.linkedin.com/jobs/view/4477455763).",
                 "https://www.linkedin.com/jobs/view/4477455763")):
            with self.subTest(raw=raw):
                self.assertEqual(self.ib._clean_url(raw), want)

    # ---- recommendation blocks -------------------------------------------
    def _msg(self, *ids, heading="similar jobs you may be interested in"):
        parts = []
        for n, jid in enumerate(ids):
            if heading and n == 1:
                parts.append(f"<h3>{heading}</h3>")
            parts.append(f'<a href="https://www.linkedin.com/jobs/view/{jid}">'
                         f'job {jid}</a>')
        return "".join(parts)

    def test_jobs_under_a_suggestion_heading_are_identified(self):
        got = self.ib._reco_ids(self._msg("111", "222", "333"))
        self.assertEqual(got, {"222", "333"})

    def test_a_heading_before_any_job_relabels_nothing(self):
        """A digest whose own header matches must not have every row
        relabelled - and, when this filtering removed rows rather than
        labelling them, must not be emptied."""
        html = ('<h3>similar jobs you may be interested in</h3>'
                '<a href="https://www.linkedin.com/jobs/view/111">a</a>')
        self.assertEqual(self.ib._reco_ids(html), set())

    def test_no_heading_means_nothing_is_a_suggestion(self):
        self.assertEqual(self.ib._reco_ids(self._msg("111", "222",
                                                     heading=None)), set())
        self.assertEqual(self.ib._reco_ids(""), set())
        self.assertEqual(self.ib._reco_ids(None), set())

    def test_a_job_above_and_below_the_heading_is_not_a_suggestion(self):
        """LinkedIn repeats a job inside its own suggestion block. If it
        arrived as a real alert row too, that is what it is."""
        html = (self._msg("111", "222") +
                '<a href="https://www.linkedin.com/jobs/view/111">again</a>')
        self.assertNotIn("111", self.ib._reco_ids(html))

    # ---- already applied --------------------------------------------------
    def test_the_applied_marker_needs_its_date(self):
        """LinkedIn's confirmation mail says "Applied on October 28, 2026" on
        the job you just applied to. A bare "Applied" is not enough - it shows
        up in prose - and dropping a real posting is the worse error."""
        self.assertTrue(self.ib.LI_APPLIED.search("Applied on October 28, 2026"))
        self.assertFalse(self.ib.LI_APPLIED.search("Applied Sciences"))
        self.assertFalse(self.ib.LI_APPLIED.search("applied research methods"))
        self.assertFalse(self.ib.LI_APPLIED.search("Applied"))


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


class TestSuccessFactorsFeed(unittest.TestCase):
    """RMK's whole-board RSS feed - the "feed": true target mode. No network.

    Deloitte Canada is only reachable this way: its search page renders no
    rows server-side and the XHR endpoint it uses is robots-disallowed.
    """

    def setUp(self):
        self.sf = adapters.platform("successfactors")

    def test_a_feed_target_is_not_fanned_out_per_keyword(self):
        """Ten tasks would mean ten copies of the same 9 MB download."""
        tok = {"base": "https://careers.deloitte.ca", "feed": True}
        self.assertEqual(adapters.expand("successfactors", tok), [tok])

    def test_a_search_target_is_still_fanned_out(self):
        out = adapters.expand("successfactors", {"base": "b"})
        self.assertEqual(len(out), len(self.sf.SF_QUERIES))
        self.assertEqual({q["_query"] for q in out}, set(self.sf.SF_QUERIES))

    def test_the_whole_description_survives_the_tag_stripper(self):
        """The trap this adapter exists to avoid.

        <description> is a CDATA block of ESCAPED html, so its only literal
        angle brackets are the CDATA delimiters - and fc.TAG is
        <[^>]+>, which therefore matches the entire description as ONE tag.
        Strip first and a 14 KB job description becomes one space, leaving
        every posting to be scored on its title alone. Unescaping first is
        load-bearing, not tidiness.
        """
        item = ("<item><description><![CDATA[&lt;p&gt;We run &lt;b&gt;"
                "Kubernetes&lt;/b&gt; on AWS with Terraform.&lt;/p&gt;]]>"
                "</description></item>")
        raw = re.search(r"<description>(.*)</description>", item).group(1)
        self.assertEqual(len(fc.strip_html(raw)), 1)      # the bug
        got = fc.strip_html(self.sf._feed_field(item, "description"))
        self.assertIn("Kubernetes", got)
        self.assertIn("Terraform", got)
        self.assertNotIn("<", got)
        self.assertNotIn("&lt;", got)

    def test_a_field_outside_cdata_is_unescaped_too(self):
        item = "<item><title>Ops &amp; Platform (M&amp;A)</title></item>"
        self.assertEqual(self.sf._feed_field(item, "title"),
                         "Ops & Platform (M&A)")

    def test_a_missing_field_is_empty_not_none(self):
        self.assertEqual(self.sf._feed_field("<item></item>", "link"), "")

    def test_namespaced_fields_are_readable(self):
        item = "<item><g:location>Calgary, AB, CA, T2P 5C5</g:location></item>"
        self.assertEqual(self.sf._feed_field(item, "g:location"),
                         "Calgary, AB, CA, T2P 5C5")

    def test_postal_codes_and_repeats_leave_the_location(self):
        """Noise in the tracker and in a push notification. Verified against
        all 18 distinct values on Deloitte's board: no classification moves."""
        for raw, want in (
                ("Toronto, ON, CA, M5H 0A9", "Toronto, ON, CA"),
                ("Toronto, ON, ON, CA, M5H 0A9", "Toronto, ON, CA"),
                ("Calgary, AB, AB, CA, T2P 5C5", "Calgary, AB, CA"),
                ("St. John's, NL, CA, A1B 3M2", "St. John's, NL, CA"),
                ("", "")):
            with self.subTest(raw=raw):
                self.assertEqual(self.sf._feed_loc(raw), want)

    def test_tidying_a_location_never_changes_its_bucket(self):
        for raw in ("Toronto, ON, CA, M5H 0A9", "Montréal, QC, CA, H3B 4T9",
                    "Vancouver, BC, CA, V7X 1P4", "Halifax, NS, CA, B3J 3R7"):
            with self.subTest(raw=raw):
                self.assertEqual(jr.classify_location(raw),
                                 jr.classify_location(
                                     self.sf._feed_loc(raw)))
                self.assertEqual(jr.provinces_of(raw),
                                 jr.provinces_of(
                                     self.sf._feed_loc(raw)))

    def test_a_postal_code_is_not_mistaken_for_a_city(self):
        """SF_POSTAL must match a postal code and nothing that reads as
        placename - 'Quebec City' and a bare province must survive."""
        self.assertTrue(self.sf.SF_POSTAL.match("M5H 0A9"))
        self.assertTrue(self.sf.SF_POSTAL.match("T2P5C5"))
        for keep in ("Toronto", "ON", "CA", "Quebec City", "St. John's"):
            with self.subTest(keep=keep):
                self.assertFalse(self.sf.SF_POSTAL.match(keep))


class TestRuleData(unittest.TestCase):
    """The shipped rules/*.json, checked through the public surface.

    These are the two things in the radar that get tuned, so they moved out of
    job_radar.py and into data. That makes a tuning mistake a data error
    instead of a syntax error in the engine - but only if the data is checked,
    which is what this is for.
    """

    TITLES = os.path.join(HERE, "rules", "titles.json")
    SKILLS = os.path.join(HERE, "rules", "skills.json")

    def test_every_title_rule_compiles(self):
        for rx, tier, fam in jr.TITLE_RULES:
            with self.subTest(rx=rx):
                re.compile(rx)
                self.assertIn(tier, (2, 3, 4))
                self.assertIn(fam, ("devops", "adjacent", "vendor"))

    def test_one_surface_form_belongs_to_one_skill(self):
        """The invariant the 2026-10-22 restructure existed for. Two groups
        claiming a form is how "Kubernetes (K8s)" scored one skill twice, at
        two different weights, inflating 45 of 177 tracker rows."""
        seen = {}
        for canon, _w, forms in jr.SKILL_GROUPS:
            for f in forms:
                with self.subTest(form=f):
                    self.assertNotIn(f, seen,
                                     f"{f!r} is in both {seen.get(f)!r} "
                                     f"and {canon!r}")
                seen[f] = canon
        self.assertEqual(len(seen), len(jr.SKILLS))

    def test_no_skill_group_is_formless(self):
        """'|'.join(()) is '', re.compile('') matches every string, and the
        group would then score on every JD ever read."""
        for canon, w, forms in jr.SKILL_GROUPS:
            with self.subTest(skill=canon):
                self.assertTrue(forms)
                self.assertIn(w, (1, 2, 3, 4))
                self.assertTrue(all(isinstance(f, str) and f for f in forms))

    def test_flat_SKILLS_agrees_with_the_groups(self):
        """hn.py ranks Hacker News comments off the flat dict, so the two
        lanes must not disagree about what a skill is worth."""
        for canon, w, forms in jr.SKILL_GROUPS:
            for f in forms:
                with self.subTest(form=f):
                    self.assertEqual(jr.SKILLS[f], w)

    def test_the_data_files_keep_their_reasoning(self):
        """Every weight here was measured against a real corpus, and the
        measurement is recorded in `_doc` and per-entry `note` fields. A rule
        whose justification has been lost is a rule nobody can safely change,
        so the notes are asserted rather than hoped for."""
        for path, key, floor in ((self.TITLES, "rules", 20),
                                 (self.SKILLS, "groups", 50)):
            with self.subTest(path=os.path.basename(path)), open(path) as f:
                d = json.load(f)
                # _doc and note are arrays of lines, so that a one-line
                # change to a note is a one-line diff rather than a whole
                # re-escaped paragraph
                self.assertGreater(len("\n".join(d["_doc"])), 400)
                self.assertGreaterEqual(len(d[key]), floor)
                notes = [e["note"] for e in d[key] if "note" in e]
                self.assertTrue(notes)
                self.assertTrue(all(isinstance(n, list) for n in notes))
                self.assertTrue(any("section" in e for e in d[key]))

    def test_the_files_are_what_the_module_loaded(self):
        """Catches a hand-edited rules file that was never reloaded, and any
        drift between the data and the tuple shape the engine expects."""
        with open(self.TITLES) as f:
            self.assertEqual(
                [(r["re"], r["tier"], r["family"]) for r in json.load(f)["rules"]],
                [tuple(x) for x in jr.TITLE_RULES])
        with open(self.SKILLS) as f:
            self.assertEqual(
                [(g["skill"], g["weight"], tuple(g["forms"]))
                 for g in json.load(f)["groups"]],
                list(jr.SKILL_GROUPS))


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

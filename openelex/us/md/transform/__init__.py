from datetime import datetime
import logging

from nameparser import HumanName

from openelex.base.transform import registry
from openelex.models import Candidate, Contest, Office, Party, RawResult, Result


PARTY_MAP = {
    'BOT': 'UNF',
    'Democratic': 'DEM',
    'Republican': 'REP',
    'Libertarian': 'LIB',
    'Green': 'GRN',
    'Unaffiliated': 'UNF',
}
"""
Map of party values as they appear in MD raw results to canonical
abbreviations.

In 2002, the values are party names.  Map them to abbreviations.

From 2003 onward, the values are party abbreviations and in most
cases match the canonical abbreviations.
"""

# Lists of fields on RawResult that are contributed to the canonical
# models.  Maybe this makes more sense to be in the model.

# These get copied onto all the related models
meta_fields = ['source', 'election_id', 'state',]
contest_fields = meta_fields + ['start_date', 'end_date',
    'election_type', 'primary_type', 'result_type', 'special',]
candidate_fields = meta_fields + ['full_name', 'given_name', 
    'family_name', 'additional_name']
result_fields = meta_fields + ['reporting_level', 'jurisdiction',
    'votes', 'total_votes', 'vote_breakdowns']

district_offices = [
    "U.S. House of Representatives",
    "State Senate",
    "House of Delegates",
]

# Caches to avoid hitting the database
office_cache = {}
party_cache = {}

def _get_fields(raw_result, field_names):
    """
    Extract the fields from a RawResult that will be used to
    construct a related model.

    Returns a dict of fields and values that can be passed to the
    model constructor.
    """
    return { k:getattr(raw_result, k) for k in field_names } 


def _clean_office(office):
    lc_office = office.lower()
    if "president" in lc_office:
        return "President" 
    elif "u.s. senat" in lc_office:
        return "U.S. Senate"
    elif "congress" in lc_office:
        return "U.S. House of Representatives"
    elif "state senat" in lc_office:
        # Match both "State Senate" and "State Senator"
        return "State Senate"
    elif "governor" in lc_office:
        return "Governor"

    return office

def _clean_party(party):
    if party == 'Both Parties':
        # 2002 candidates have "Both Parties" in the write-in
        # field
        # TODO: Is this the right way to handle this?
        return None

    try:
        return PARTY_MAP[party]
    except KeyError:
        return party

def _strip_leading_zeros(val):
    return val.lstrip("0")

def _get_office(raw_result):
    office_query = {
        'state': 'MD',
        'name': _clean_office(raw_result.office),
    }

    # Handle president, where state = "US" 
    if office_query['name'] == "President":
        office_query['state'] = "US"

    if office_query['name'] in district_offices:
        office_query['district'] = _strip_leading_zeros(raw_result.district)

    key = Office.make_key(**office_query)
    try:
        return office_cache[key]
    except KeyError:
        try:
            office = Office.objects.get(**office_query)
            # TODO: Remove this once I'm sure this always works. It should.
            assert key == office.key
            office_cache[key] = office
            return office
        except Office.DoesNotExist:
            print "No office matching query %s" % (office_query)
            raise

def _get_party(raw_result, attr='party'):
    party = getattr(raw_result, attr)
    if not party:
        return None

    clean_abbrev = _clean_party(party)
    if not clean_abbrev:
        return None

    try:
        return party_cache[clean_abbrev]
    except KeyError:
        try:
            party = Party.objects.get(abbrev=clean_abbrev)
            party_cache[clean_abbrev] = party
            return party
        except Party.DoesNotExist:
            print "No party with abbreviation %s" % (clean_abbrev)
            raise

def get_raw_results_after_2000():
    # Filter raw results for everything newer than 2002, inclusive
    return RawResult.objects.filter(state='MD',
        end_date__gte=datetime(2002, 1, 1))

def get_results_after_2000():
    election_ids = get_raw_results_after_2000().distinct('election_id')
    return Result.objects.filter(election_id__in=election_ids)

def get_contest_fields(raw_result):
    # Resolve Office and Party related objects
    fields = _get_fields(raw_result, contest_fields)
    if not fields['primary_type']:
        del fields['primary_type']
    fields['office'] = _get_office(raw_result)
    fields['primary_party'] = _get_party(raw_result, 'primary_party')
    return fields

def contest_key(raw_result):
    # HACK: Work around districts put in presidential races in a few cases
    slug = raw_result.contest_slug
    if (_clean_office(raw_result.office) not in district_offices and
        raw_result.district):  
        slug = slug.replace('-' + raw_result.district.lower(), '')
    return (raw_result.election_id, slug)

def create_unique_contests_after_2000():
    contests = []
    seen = set()

    for rr in get_raw_results_after_2000():
        key = contest_key(rr)
        if key not in seen:
            fields = get_contest_fields(rr)
            fields['updated'] = fields['created'] = datetime.now()
            contest = Contest(**fields)
            contests.append(contest)
            seen.add(key)

    Contest.objects.insert(contests, load_bulk=False)

    print "Created %d contests." % len(contests) 

def cached_get_contest(raw_result, cache):
    key = "%s-%s" % (raw_result.election_id, raw_result.contest_slug)
    try:
        return cache[key]
    except KeyError:
        fields = get_contest_fields(raw_result)
        fields.pop('source')
        contest = Contest.objects.get(**fields)
        cache[key] = contest
        return contest

def get_candidate_fields(raw_result):
    year = raw_result.end_date.year
    if year == 2002:
        return get_candidate_fields_2002(raw_result)
    elif year >= 2003:
        return get_candidate_fields_after_2000(raw_result)
    else:
        raise ValueError

def get_candidate_fields_2002(raw_result):
    fields = _get_fields(raw_result, candidate_fields)
    if fields['family_name'] == 'zz998':
        # Write-In
        del fields['family_name']
        del fields['given_name']
        del fields['additional_name']
        fields['full_name'] =  "Other Write-Ins"
    else:
        bits = [fields['given_name']]
        if fields['additional_name'] == '\\N':
            # Null last name
            del fields['additional_name']
        else:
            bits.append(fields['additional_name'])
        bits.append(fields['family_name'])   
        fields['full_name'] = ' '.join(bits)

    return fields

def get_candidate_fields_after_2000(raw_result):
    fields = _get_fields(raw_result, candidate_fields)
    if fields['full_name'] == "Other Write-Ins":
        return fields

    name = HumanName(raw_result.full_name)
    fields['given_name'] = name.first
    fields['family_name'] = name.last
    fields['additional_name'] = name.middle
    fields['suffix'] = name.suffix
    return fields

def create_unique_candidates_after_2000():
    contest_cache = {}
    candidates = []
    seen = set()

    for rr in get_raw_results_after_2000():
        key = (rr.election_id, rr.candidate_slug)
        if key not in seen:
            fields = get_candidate_fields(rr)
            fields['contest'] = cached_get_contest(rr, contest_cache) 
            if "other" in fields['full_name'].lower():
                if fields['full_name'] == "Other Write-Ins":
                    fields['flags'] = ['aggregate',]
                else:
                    # As far as I can tell the value should always be 
                    # "Other Write-Ins", but output a warning to let us know
                    # about some cases we may be missing.
                    logging.warn("'other' found in candidate name field value: "
                            "'%s'" % rr.full_name)
            candidate = Candidate(**fields)
            candidates.append(candidate)
            seen.add(key)

    Candidate.objects.insert(candidates, load_bulk=False)

    print "Created %d candidates." % len(candidates) 

def _parse_winner(raw_result):
    """
    Converts raw winner value into boolean
    """
    if raw_result.winner == 'Y':
        # Winner in post-2002 contest
        return True
    elif raw_result.winner == 1:
        # Winner in 2002 contest
        return True
    else:
        return False

def _parse_write_in(raw_result):
    """
    Converts raw winner value into boolean
    """
    if raw_result.write_in == 'Y':
        # Write-in in post-2002 contest
        return True
    elif raw_result.family_name == 'zz998':
        # Write-in in 2002 contest
        return True
    else:
        return False

def _get_ocd_id(raw_result):
    clean_jurisdiction = _strip_leading_zeros(raw_result.jurisdiction)
    if raw_result.reporting_level == "county":
        # TODO: Should jurisdiction/ocd_id be different for Baltimore City?
        # TODO: Slugify county name 
        return "ocd-division/country:us/state:md/county:%s" % clean_jurisdiction, 
    elif raw_result.reporting_level == "state_legislative":
        return "ocd-division/country:us/state:md/sldl:%s" % clean_jurisdiction
    elif raw_result.reporting_level == "precinct": 
        return "%s/precinct:%s" % (raw_result.county_ocd_id, clean_jurisdiction)
    else: 
        return None

def cached_get_candidate(raw_result, cache):
    key = "%s-%s" % (raw_result.election_id, raw_result.candidate_slug)
    try:
        return cache[key]
    except KeyError:
        fields = get_candidate_fields(raw_result)
        try:
            candidate = Candidate.objects.get(**fields)
        except Candidate.DoesNotExist:
            print fields 
            raise
        cache[key] = candidate 
        return candidate

def create_unique_results_after_2000():
    candidate_cache = {}
    results = []
    num_created = 0
    # Number of records to insert at once.  We need to do this because
    # the number of records we create will exceed with what Mongo can
    # do in a single call to QuerySet.insert().  
    #
    # 1000 is a totally arbitrary size 
    bufsiz = 1000

    # Delete existing results
    old_results = get_results_after_2000()
    print "\tDeleting %d previously loaded results" % old_results.count() 
    old_results.delete()

    for rr in get_raw_results_after_2000():
        fields = _get_fields(rr, result_fields)
        fields['candidate'] = cached_get_candidate(rr, candidate_cache)
        fields['contest'] = fields['candidate'].contest 
        fields['raw_result'] = rr
        party = _get_party(rr)
        if party:
            fields['party'] = party.abbrev
        fields['winner'] = _parse_winner(rr)
        fields['write_in'] = _parse_write_in(rr)
        fields['jurisdiction'] = _strip_leading_zeros(rr.jurisdiction)
        fields['ocd_id'] = _get_ocd_id(rr)
        result = Result(**fields)
        results.append(result)
        if len(results) >= bufsiz:
            Result.objects.insert(results, load_bulk=False)
            num_created += len(results)
            results = []

    if len(results):
        Result.objects.insert(results, load_bulk=False)
        num_created += len(results)

    print "Created %d results." % num_created 
        

# TODO: When should we create a Person

#def standardize_office_and_district():
#    pass

#def clean_vote_counts():
    #pass

registry.register('md', create_unique_contests_after_2000)
registry.register('md', create_unique_candidates_after_2000)
registry.register('md', create_unique_results_after_2000)
#registry.register('md', standardize_office_and_district)
#registry.register('md', clean_vote_counts)

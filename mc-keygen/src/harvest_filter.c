#include "../cuda/harvest_filter.h"

int mc_harvest_candidate(const unsigned char *key, const unsigned int *policy,
                         const unsigned int *trie) {
    return harvest_candidate(key, policy, trie);
}

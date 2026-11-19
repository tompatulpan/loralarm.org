CXX ?= g++
CXXFLAGS ?= -std=c++11 -Wall -Wextra -Werror -O0

all: run

POLICY_HDRS = ../../battery_policy.h ../../downlink_parse.h ../../send_policy.h

test_policies: test_policies.cpp $(POLICY_HDRS)
	$(CXX) $(CXXFLAGS) -o $@ test_policies.cpp

test_payload_budget: test_payload_budget.cpp ../../payload_budget.h
	$(CXX) $(CXXFLAGS) -o $@ test_payload_budget.cpp

run: test_policies test_payload_budget
	./test_policies
	./test_payload_budget

clean:
	rm -f test_policies test_payload_budget

.PHONY: all run clean

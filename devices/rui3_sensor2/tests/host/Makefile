CXX ?= g++
CXXFLAGS ?= -std=c++11 -Wall -Wextra -Werror -O0

all: run

test_policies: test_policies.cpp ../../battery_policy.h ../../downlink_parse.h ../../send_policy.h
	$(CXX) $(CXXFLAGS) -o $@ test_policies.cpp

run: test_policies
	./test_policies

clean:
	rm -f test_policies

.PHONY: all run clean

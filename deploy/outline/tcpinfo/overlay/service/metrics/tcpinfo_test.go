// Copyright 2026 Keel contributors
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package metrics

import (
	"errors"
	"net"
	"sync"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/stretchr/testify/require"
)

type fixtureTCPSampler struct {
	mu     sync.Mutex
	sample tcpInfoSample
	err    error
	calls  int
}

func (s *fixtureTCPSampler) Sample() (tcpInfoSample, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.calls++
	return s.sample, s.err
}

func (s *fixtureTCPSampler) set(sample tcpInfoSample, err error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.sample = sample
	s.err = err
}

func (s *fixtureTCPSampler) callCount() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.calls
}

func newFixtureTCPCollector(samplers ...tcpInfoSampler) *TCPInfoCollector {
	next := 0
	return newTCPInfoCollectorWithSampler(func(net.Conn) tcpInfoSampler {
		if next >= len(samplers) {
			return nil
		}
		sampler := samplers[next]
		next++
		return sampler
	})
}

type tcpMetricID struct {
	name string
	key  string
	leg  string
}

func gatherTCPMetrics(t *testing.T, registry *prometheus.Registry) map[tcpMetricID]float64 {
	t.Helper()
	families, err := registry.Gather()
	require.NoError(t, err)
	values := make(map[tcpMetricID]float64)
	for _, family := range families {
		for _, metric := range family.GetMetric() {
			id := tcpMetricID{name: family.GetName()}
			for _, label := range metric.GetLabel() {
				require.Contains(t, []string{"access_key", "leg"}, label.GetName())
				switch label.GetName() {
				case "access_key":
					id.key = label.GetValue()
				case "leg":
					id.leg = label.GetValue()
				}
			}
			if id.name == "keel_tcp_device_notsent_bytes" || id.name == "keel_tcp_device_retrans_bytes_total" {
				require.Len(t, metric.GetLabel(), 1)
			} else {
				require.Len(t, metric.GetLabel(), 2)
			}
			switch {
			case metric.GetGauge() != nil:
				values[id] = metric.GetGauge().GetValue()
			case metric.GetCounter() != nil:
				values[id] = metric.GetCounter().GetValue()
			default:
				t.Fatalf("unexpected metric type for %s", family.GetName())
			}
		}
	}
	return values
}

func TestTCPInfoCollectorInitialZeroes(t *testing.T) {
	collector := newFixtureTCPCollector()
	registry := prometheus.NewPedanticRegistry()
	registry.MustRegister(collector)

	collector.AddDevice("key-1", nil)

	require.Empty(t, gatherTCPMetrics(t, registry))
}

func TestTCPInfoCollectorDeltasFailuresAndFinalSamples(t *testing.T) {
	deviceSampler := &fixtureTCPSampler{sample: tcpInfoSample{
		rtt: 20 * time.Millisecond, bytesAcked: 100, bytesRetrans: 5, notsent: 7,
	}}
	sourceSampler := &fixtureTCPSampler{sample: tcpInfoSample{
		rtt: 10 * time.Millisecond, bytesReceived: 200,
	}}
	collector := newFixtureTCPCollector(deviceSampler, sourceSampler)
	registry := prometheus.NewPedanticRegistry()
	registry.MustRegister(collector)
	device := collector.AddDevice("key-1", nil)
	source := collector.AddSource("key-1", nil)

	values := gatherTCPMetrics(t, registry)
	require.Equal(t, 100.0, values[tcpMetricID{name: "keel_tcp_download_bytes_total", key: "key-1", leg: "device"}])
	require.Equal(t, 200.0, values[tcpMetricID{name: "keel_tcp_download_bytes_total", key: "key-1", leg: "source"}])
	require.Equal(t, 5.0, values[tcpMetricID{name: "keel_tcp_device_retrans_bytes_total", key: "key-1"}])
	require.Equal(t, 0.020, values[tcpMetricID{name: "keel_tcp_rtt_seconds_sum", key: "key-1", leg: "device"}])
	require.Equal(t, 0.010, values[tcpMetricID{name: "keel_tcp_rtt_seconds_sum", key: "key-1", leg: "source"}])
	require.Equal(t, 1.0, values[tcpMetricID{name: "keel_tcp_rtt_samples", key: "key-1", leg: "device"}])
	require.Equal(t, 7.0, values[tcpMetricID{name: "keel_tcp_device_notsent_bytes", key: "key-1"}])

	deviceSampler.set(tcpInfoSample{
		rtt: 30 * time.Millisecond, bytesAcked: 160, bytesRetrans: 9, notsent: 3,
	}, nil)
	sourceSampler.set(tcpInfoSample{bytesReceived: 1000}, errors.New("socket failed"))
	values = gatherTCPMetrics(t, registry)
	require.Equal(t, 160.0, values[tcpMetricID{name: "keel_tcp_download_bytes_total", key: "key-1", leg: "device"}])
	require.NotContains(t, values, tcpMetricID{name: "keel_tcp_download_bytes_total", key: "key-1", leg: "source"})
	require.Equal(t, 9.0, values[tcpMetricID{name: "keel_tcp_device_retrans_bytes_total", key: "key-1"}])
	require.Equal(t, 0.030, values[tcpMetricID{name: "keel_tcp_rtt_seconds_sum", key: "key-1", leg: "device"}])
	require.Zero(t, values[tcpMetricID{name: "keel_tcp_rtt_seconds_sum", key: "key-1", leg: "source"}])
	require.Zero(t, values[tcpMetricID{name: "keel_tcp_rtt_samples", key: "key-1", leg: "source"}])
	require.Equal(t, 3.0, values[tcpMetricID{name: "keel_tcp_device_notsent_bytes", key: "key-1"}])

	values = gatherTCPMetrics(t, registry)
	require.Equal(t, 160.0, values[tcpMetricID{name: "keel_tcp_download_bytes_total", key: "key-1", leg: "device"}])
	require.Equal(t, 9.0, values[tcpMetricID{name: "keel_tcp_device_retrans_bytes_total", key: "key-1"}])

	deviceSampler.set(tcpInfoSample{
		rtt: 40 * time.Millisecond, bytesAcked: 175, bytesRetrans: 12,
	}, nil)
	sourceSampler.set(tcpInfoSample{
		rtt: 25 * time.Millisecond, bytesReceived: 275,
	}, nil)
	device.Close()
	source.Close()
	deviceCalls := deviceSampler.callCount()
	sourceCalls := sourceSampler.callCount()
	device.Close()
	source.Close()
	require.Equal(t, deviceCalls, deviceSampler.callCount())
	require.Equal(t, sourceCalls, sourceSampler.callCount())

	values = gatherTCPMetrics(t, registry)
	require.Equal(t, 175.0, values[tcpMetricID{name: "keel_tcp_download_bytes_total", key: "key-1", leg: "device"}])
	require.Equal(t, 275.0, values[tcpMetricID{name: "keel_tcp_download_bytes_total", key: "key-1", leg: "source"}])
	require.Equal(t, 12.0, values[tcpMetricID{name: "keel_tcp_device_retrans_bytes_total", key: "key-1"}])
	for _, leg := range []string{"device", "source"} {
		require.Zero(t, values[tcpMetricID{name: "keel_tcp_rtt_seconds_sum", key: "key-1", leg: leg}])
		require.Zero(t, values[tcpMetricID{name: "keel_tcp_rtt_samples", key: "key-1", leg: leg}])
	}
	require.Zero(t, values[tcpMetricID{name: "keel_tcp_device_notsent_bytes", key: "key-1"}])
}

func TestTCPInfoCollectorAggregatesCurrentSockets(t *testing.T) {
	firstSampler := &fixtureTCPSampler{sample: tcpInfoSample{
		rtt: 10 * time.Millisecond, bytesAcked: 100, bytesRetrans: 1, notsent: 2,
	}}
	secondSampler := &fixtureTCPSampler{sample: tcpInfoSample{
		rtt: 30 * time.Millisecond, bytesAcked: 300, bytesRetrans: 3, notsent: 4,
	}}
	collector := newFixtureTCPCollector(firstSampler, secondSampler)
	registry := prometheus.NewPedanticRegistry()
	registry.MustRegister(collector)
	first := collector.AddDevice("key-1", nil)
	second := collector.AddDevice("key-1", nil)

	values := gatherTCPMetrics(t, registry)
	require.Equal(t, 0.040, values[tcpMetricID{name: "keel_tcp_rtt_seconds_sum", key: "key-1", leg: "device"}])
	require.Equal(t, 2.0, values[tcpMetricID{name: "keel_tcp_rtt_samples", key: "key-1", leg: "device"}])
	require.Equal(t, 400.0, values[tcpMetricID{name: "keel_tcp_download_bytes_total", key: "key-1", leg: "device"}])
	require.Equal(t, 4.0, values[tcpMetricID{name: "keel_tcp_device_retrans_bytes_total", key: "key-1"}])
	require.Equal(t, 6.0, values[tcpMetricID{name: "keel_tcp_device_notsent_bytes", key: "key-1"}])

	first.Close()
	values = gatherTCPMetrics(t, registry)
	require.Equal(t, 0.030, values[tcpMetricID{name: "keel_tcp_rtt_seconds_sum", key: "key-1", leg: "device"}])
	require.Equal(t, 1.0, values[tcpMetricID{name: "keel_tcp_rtt_samples", key: "key-1", leg: "device"}])
	require.Equal(t, 400.0, values[tcpMetricID{name: "keel_tcp_download_bytes_total", key: "key-1", leg: "device"}])
	require.Equal(t, 4.0, values[tcpMetricID{name: "keel_tcp_device_notsent_bytes", key: "key-1"}])
	second.Close()
	require.Empty(t, collector.active)
	require.Len(t, collector.totals, 1)
}

func TestTCPInfoCollectorIgnoresUnauthenticatedStreams(t *testing.T) {
	sampler := &fixtureTCPSampler{sample: tcpInfoSample{bytesAcked: 100}}
	collector := newFixtureTCPCollector(sampler)
	registry := prometheus.NewPedanticRegistry()
	registry.MustRegister(collector)

	stream := collector.AddDevice("", nil)
	stream.Close()

	require.Empty(t, gatherTCPMetrics(t, registry))
	require.Zero(t, sampler.callCount())
}

func TestFailedDeviceSampleOmitsAggregateQueueAndRecovers(t *testing.T) {
	a := &fixtureTCPSampler{sample: tcpInfoSample{notsent: 11}}
	b := &fixtureTCPSampler{sample: tcpInfoSample{notsent: 5}}
	c := &fixtureTCPSampler{sample: tcpInfoSample{notsent: 7}}
	collector := newFixtureTCPCollector(a, b, c)
	registry := prometheus.NewPedanticRegistry()
	registry.MustRegister(collector)
	first := collector.AddDevice("one", nil)
	defer first.Close()
	second := collector.AddDevice("one", nil)
	defer second.Close()
	third := collector.AddDevice("two", nil)
	defer third.Close()
	one := tcpMetricID{name: "keel_tcp_device_notsent_bytes", key: "one"}
	two := tcpMetricID{name: "keel_tcp_device_notsent_bytes", key: "two"}
	require.Equal(t, 16.0, gatherTCPMetrics(t, registry)[one])
	b.set(tcpInfoSample{}, errors.New("socket not readable"))
	values := gatherTCPMetrics(t, registry)
	require.NotContains(t, values, one)
	require.Equal(t, 7.0, values[two])
	b.set(tcpInfoSample{notsent: 3}, nil)
	require.Equal(t, 14.0, gatherTCPMetrics(t, registry)[one])
}

type onSampleFunc func() (tcpInfoSample, error)

func (f onSampleFunc) Sample() (tcpInfoSample, error) { return f() }

func TestConnectionAddedDuringScrapeDoesNotHideQueue(t *testing.T) {
	var collector *TCPInfoCollector
	var addOnce sync.Once
	registerLater := onSampleFunc(func() (tcpInfoSample, error) {
		addOnce.Do(func() { collector.AddDevice("one", nil) })
		return tcpInfoSample{notsent: 8}, nil
	})
	samplers := []tcpInfoSampler{registerLater, &fixtureTCPSampler{sample: tcpInfoSample{notsent: 7}}}
	collector = newTCPInfoCollectorWithSampler(func(net.Conn) tcpInfoSampler {
		result := samplers[0]
		samplers = samplers[1:]
		return result
	})
	collector.AddDevice("one", nil)
	registry := prometheus.NewPedanticRegistry()
	registry.MustRegister(collector)
	key := tcpMetricID{name: "keel_tcp_device_notsent_bytes", key: "one"}
	require.Equal(t, 8.0, gatherTCPMetrics(t, registry)[key])
	require.Equal(t, 15.0, gatherTCPMetrics(t, registry)[key])
}

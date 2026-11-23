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
	"net"
	"sort"
	"sync"
	"time"

	"github.com/prometheus/client_golang/prometheus"
)

const (
	tcpDeviceLeg = "device"
	tcpSourceLeg = "source"
)

var (
	tcpRTTSumDesc = prometheus.NewDesc(
		"keel_tcp_rtt_seconds_sum",
		"Sum of smoothed TCP round-trip times for currently open sockets.",
		[]string{"access_key", "leg"}, nil,
	)
	tcpRTTSamplesDesc = prometheus.NewDesc(
		"keel_tcp_rtt_samples",
		"Number of currently open sockets with a valid smoothed TCP round-trip time.",
		[]string{"access_key", "leg"}, nil,
	)
	tcpDownloadBytesDesc = prometheus.NewDesc(
		"keel_tcp_download_bytes_total",
		"TCP payload bytes downloaded, measured by the kernel.",
		[]string{"access_key", "leg"}, nil,
	)
	tcpDeviceRetransBytesDesc = prometheus.NewDesc(
		"keel_tcp_device_retrans_bytes_total",
		"TCP payload bytes retransmitted on device connections.",
		[]string{"access_key"}, nil,
	)
	tcpDeviceNotsentBytesDesc = prometheus.NewDesc(
		"keel_tcp_device_notsent_bytes",
		"Current bytes not yet sent on device connections.",
		[]string{"access_key"}, nil,
	)
)

type tcpInfoSample struct {
	rtt           time.Duration
	bytesAcked    uint64
	bytesReceived uint64
	bytesRetrans  uint64
	notsent       uint64
}

type tcpInfoSampler interface {
	Sample() (tcpInfoSample, error)
}

type tcpInfoSamplerFactory func(net.Conn) tcpInfoSampler

type tcpInfoTotals struct {
	download [2]uint64
	seen     [2]bool
	retrans  uint64
}

// TCPInfoCollector aggregates kernel TCP measurements by authenticated access key.
type TCPInfoCollector struct {
	mu         sync.RWMutex
	totals     map[string]*tcpInfoTotals
	active     map[*TCPInfoStream]struct{}
	newSampler tcpInfoSamplerFactory
}

var _ prometheus.Collector = (*TCPInfoCollector)(nil)

func NewTCPInfoCollector() *TCPInfoCollector {
	return newTCPInfoCollectorWithSampler(newTCPInfoSampler)
}

func newTCPInfoCollectorWithSampler(factory tcpInfoSamplerFactory) *TCPInfoCollector {
	return &TCPInfoCollector{
		totals:     make(map[string]*tcpInfoTotals),
		active:     make(map[*TCPInfoStream]struct{}),
		newSampler: factory,
	}
}

// TCPInfoStream holds the kernel counter baseline for one socket.
type TCPInfoStream struct {
	collector *TCPInfoCollector
	accessKey string
	leg       string
	sampler   tcpInfoSampler

	mu           sync.Mutex
	closed       bool
	lastDownload uint64
	lastRetrans  uint64
	rtt          time.Duration
	rttValid     bool
	sampleValid  bool
	notsent      uint64
}

func (c *TCPInfoCollector) AddDevice(accessKey string, conn net.Conn) *TCPInfoStream {
	return c.addStream(accessKey, tcpDeviceLeg, conn)
}

func (c *TCPInfoCollector) AddSource(accessKey string, conn net.Conn) *TCPInfoStream {
	return c.addStream(accessKey, tcpSourceLeg, conn)
}

func (c *TCPInfoCollector) addStream(accessKey, leg string, conn net.Conn) *TCPInfoStream {
	if accessKey == "" {
		return nil
	}
	sampler := c.newSampler(conn)
	if sampler == nil {
		return nil
	}
	stream := &TCPInfoStream{
		collector: c,
		accessKey: accessKey,
		leg:       leg,
		sampler:   sampler,
	}
	c.mu.Lock()
	if _, ok := c.totals[accessKey]; !ok {
		c.totals[accessKey] = &tcpInfoTotals{}
	}
	c.active[stream] = struct{}{}
	c.mu.Unlock()
	return stream
}

// Close takes the final socket sample and removes the stream from current gauges.
// It is safe to call more than once.
func (s *TCPInfoStream) Close() {
	if s == nil {
		return
	}
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return
	}
	s.sampleLocked()
	s.closed = true
	s.rtt = 0
	s.rttValid = false
	s.notsent = 0
	s.mu.Unlock()

	s.collector.mu.Lock()
	delete(s.collector.active, s)
	s.collector.mu.Unlock()
}

func (s *TCPInfoStream) sample() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.sampleLocked()
}

func (s *TCPInfoStream) sampleLocked() {
	if s.closed || s.sampler == nil {
		return
	}
	sample, err := s.sampler.Sample()
	if err != nil {
		s.rtt = 0
		s.rttValid = false
		s.sampleValid = false
		s.notsent = 0
		return
	}

	var download, retrans, notsent uint64
	switch s.leg {
	case tcpDeviceLeg:
		download = sample.bytesAcked
		retrans = sample.bytesRetrans
		notsent = sample.notsent
	case tcpSourceLeg:
		download = sample.bytesReceived
	}
	downloadDelta := tcpCounterDelta(download, &s.lastDownload)
	retransDelta := uint64(0)
	if s.leg == tcpDeviceLeg {
		retransDelta = tcpCounterDelta(retrans, &s.lastRetrans)
	}
	s.sampleValid = true
	s.rtt = sample.rtt
	s.rttValid = sample.rtt > 0
	s.notsent = notsent

	s.collector.mu.Lock()
	total := s.collector.totals[s.accessKey]
	i := 0
	if s.leg == tcpSourceLeg {
		i = 1
	}
	total.seen[i] = true
	total.download[i] += downloadDelta
	total.retrans += retransDelta
	s.collector.mu.Unlock()
}

func tcpCounterDelta(current uint64, previous *uint64) uint64 {
	var delta uint64
	if current >= *previous {
		delta = current - *previous
	}
	*previous = current
	return delta
}

func (c *TCPInfoCollector) Describe(ch chan<- *prometheus.Desc) {
	ch <- tcpRTTSumDesc
	ch <- tcpRTTSamplesDesc
	ch <- tcpDownloadBytesDesc
	ch <- tcpDeviceRetransBytesDesc
	ch <- tcpDeviceNotsentBytesDesc
}

type tcpInfoTotalsSnapshot struct {
	accessKey string
	download  [2]uint64
	seen      [2]bool
	retrans   uint64
}

type tcpInfoCurrent struct {
	unknown bool
	rttSum  float64
	samples uint64
	notsent uint64
}

type tcpInfoCurrentID struct {
	accessKey string
	leg       string
}

func (c *TCPInfoCollector) Collect(ch chan<- prometheus.Metric) {
	c.mu.RLock()
	active := make([]*TCPInfoStream, 0, len(c.active))
	for stream := range c.active {
		active = append(active, stream)
	}
	c.mu.RUnlock()
	for _, stream := range active {
		stream.sample()
	}

	c.mu.RLock()
	totals := make([]tcpInfoTotalsSnapshot, 0, len(c.totals))
	for accessKey, total := range c.totals {
		totals = append(totals, tcpInfoTotalsSnapshot{
			accessKey: accessKey,
			download:  total.download,
			seen:      total.seen,
			retrans:   total.retrans,
		})
	}
	// A socket opened after the scrape started participates in the next scrape.
	c.mu.RUnlock()

	current := make(map[tcpInfoCurrentID]tcpInfoCurrent)
	for _, stream := range active {
		stream.mu.Lock()
		if !stream.closed {
			id := tcpInfoCurrentID{accessKey: stream.accessKey, leg: stream.leg}
			values := current[id]
			values.unknown = values.unknown || !stream.sampleValid
			if stream.rttValid {
				values.rttSum += stream.rtt.Seconds()
				values.samples++
			}
			if stream.leg == tcpDeviceLeg {
				values.notsent += stream.notsent
			}
			current[id] = values
		}
		stream.mu.Unlock()
	}

	sort.Slice(totals, func(i, j int) bool {
		return totals[i].accessKey < totals[j].accessKey
	})
	for _, total := range totals {
		for i, leg := range []string{tcpDeviceLeg, tcpSourceLeg} {
			id := tcpInfoCurrentID{accessKey: total.accessKey, leg: leg}
			values := current[id]
			if !total.seen[i] {
				continue
			}
			ch <- prometheus.MustNewConstMetric(
				tcpRTTSumDesc, prometheus.GaugeValue, values.rttSum, total.accessKey, leg,
			)
			ch <- prometheus.MustNewConstMetric(
				tcpRTTSamplesDesc, prometheus.GaugeValue, float64(values.samples), total.accessKey, leg,
			)
			if values.unknown {
				continue
			}
			ch <- prometheus.MustNewConstMetric(
				tcpDownloadBytesDesc, prometheus.CounterValue, float64(total.download[i]), total.accessKey, leg,
			)
		}
		deviceID := tcpInfoCurrentID{accessKey: total.accessKey, leg: tcpDeviceLeg}
		if !total.seen[0] || current[deviceID].unknown {
			continue
		}
		ch <- prometheus.MustNewConstMetric(
			tcpDeviceRetransBytesDesc, prometheus.CounterValue, float64(total.retrans), total.accessKey,
		)
		ch <- prometheus.MustNewConstMetric(
			tcpDeviceNotsentBytesDesc, prometheus.GaugeValue, float64(current[deviceID].notsent), total.accessKey,
		)
	}
}

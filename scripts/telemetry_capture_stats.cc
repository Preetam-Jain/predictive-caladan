// Bounded-memory analysis of version-8 through 10 telemetry; never prints raw records.
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>
#pragma pack(push,1)
struct Header{char magic[8];uint32_t version,header_bytes,record_bytes,type;double cpu;uint64_t selector;uint32_t writer,scheduler,ncores,flags;double multiplier;uint64_t produced,dropped;uint8_t uuid[16];};
struct BW{uint64_t tsc,cas;};
struct LLC{uint64_t tsc,gen,misses,app,owner_req,owner_collect,gen_collect;uint32_t core,pid;};
struct Failure{uint64_t tsc,gen;uint32_t core,reason;};
struct SnapshotFailure{uint64_t tsc;uint32_t pid,tid,core,reason;};
struct Sched{uint64_t tsc,gen;uint32_t pid;uint16_t core,reason;};
struct Queue{uint64_t tsc,runnable,rx,timer,directpath,storage;uint32_t pid,tid,core,flags;};
struct Task{uint64_t tsc,task,kthread;uint32_t pid,tid,core,rcu;};
struct Delay{uint64_t tsc,rx;double max,min,avg;uint32_t pid,active,flags,reserved;};
#pragma pack(pop)
static_assert(sizeof(Header)==96 && sizeof(LLC)==64 && sizeof(Queue)==64 && sizeof(Delay)==56);
// Exact integer histogram through 65535; larger values use <=0.4% wide bins.
struct Stats{
 std::vector<uint64_t> h=std::vector<uint64_t>(65536+64*256);uint64_t n=0,nz=0,max=0;long double sum=0;
 void add(uint64_t x){n++;nz+=x!=0;sum+=x;max=std::max(max,x);size_t b=x<65536?x:65536+size_t(std::floor((std::log2(double(x))-16)*256));h[std::min(b,h.size()-1)]++;}
 double quant(double q,bool positive=false)const{uint64_t count=positive?nz:n;if(!count)return 0;uint64_t rank=std::max<uint64_t>(1,std::ceil(q*count)),seen=0;for(size_t i=positive?1:0;i<h.size();i++){seen+=h[i];if(seen>=rank)return i<65536?double(i):std::exp2(16+(i-65536+.5)/256.);}return max;}
 void csv(std::ostream&o,const std::string&name,double divisor)const{o<<name<<','<<n<<','<<nz<<','<<(n?100.*nz/n:0)<<','<<(n?double(sum/n)/divisor:0)<<','<<quant(.5)/divisor<<','<<quant(.99)/divisor<<','<<quant(.999)/divisor<<','<<max/divisor<<','<<quant(.5,true)/divisor<<','<<quant(.99,true)/divisor<<'\n';}
};
struct Bin{long double bytes=0,bwtime=0,misses=0;uint64_t qn=0,work=0,standing=0,activeq=0,taskn=0,usern=0,delayn=0,delayvalid=0,active_sum=0,events=0,failures=0;double qmax=0,directmax=0,timermax=0,taskmax=0,delaymax=0,delaysum=0;};
struct Core{uint64_t llcn=0,valid=0,misses=0,ticks=0,qn=0,work=0,standing=0;double qmax=0;};
struct Analyzer{
 uint64_t begin,end;double hz,cpu,mult;uint32_t pid;std::string out;int seconds;std::vector<Bin> bins;
 std::map<std::string,Stats> stats;std::map<uint32_t,Core> cores;std::map<uint32_t,LLC> prev;
 std::map<std::string,uint64_t> counts;std::map<std::string,std::map<uint32_t,uint64_t>> pids;
 std::vector<Queue> outliers=std::vector<Queue>(8);
 BW lastbw{};bool havebw=false;double bwcoverage=0;long double totalbytes=0;uint64_t badtime=0,badcounter=0,badowner=0;std::map<unsigned,uint64_t> events,failures;
 bool in(uint64_t t)const{return t>=begin&&t<end;}int bin(uint64_t t)const{return std::clamp(int((t-begin)/hz),0,seconds-1);}
 template<class T,class F>void scan(const std::string&prefix,const std::string&suffix,F fn){
  std::ifstream f(prefix+'.'+suffix+".bin",std::ios::binary);if(!f)throw std::runtime_error("Missing stream "+suffix);Header h{};f.read((char*)&h,sizeof(h));
  if(std::memcmp(h.magic,"CALATLM\0",8)||(h.version<8||h.version>10)||h.record_bytes!=sizeof(T)||h.flags!=1||h.cpu!=cpu||h.dropped)throw std::runtime_error("Invalid/nonfinal/lossy stream "+suffix);
  f.seekg(h.header_bytes);std::vector<T> block(65536);uint64_t remaining=h.produced,selected=0;uint64_t first=0,last=0;
  while(remaining){size_t n=std::min<uint64_t>(remaining,block.size());if(!f.read((char*)block.data(),n*sizeof(T)))throw std::runtime_error("Truncated "+suffix);for(size_t i=0;i<n;i++){const T&r=block[i];if(!first)first=r.tsc;last=r.tsc;selected+=in(r.tsc);fn(r);}remaining-=n;}
  char extra;if(f.read(&extra,1))throw std::runtime_error("Extra stream data "+suffix);counts[suffix]=selected;std::cerr<<suffix<<": "<<selected<<" records in window; scanned "<<h.produced<<'\n';
 }
 void bandwidth(const BW&r){if(havebw&&r.tsc>lastbw.tsc){uint64_t a=std::max(begin,lastbw.tsc),b=std::min(end,r.tsc);if(b>a){double rate=uint32_t(r.cas-lastbw.cas)*(mult/cpu)*hz/double(r.tsc-lastbw.tsc);for(uint64_t t=a;t<b;){int i=bin(t);uint64_t stop=std::min(b,begin+uint64_t((i+1)*hz));double dt=(stop-t)/hz;bins[i].bytes+=rate*dt;bins[i].bwtime+=dt;totalbytes+=rate*dt;bwcoverage+=dt;t=stop;}stats["bandwidth_poll_interval_us"].add(r.tsc-lastbw.tsc);}}lastbw=r;havebw=true;}
 void llc(const LLC&r){auto it=prev.find(r.core);if(in(r.tsc)){pids["llc"][r.pid]++;cores[r.core].llcn++;}
  if(it!=prev.end()){auto&p=it->second;if(r.tsc<=p.tsc){if(in(r.tsc))badtime++;}else if(r.misses<p.misses){if(in(r.tsc))badcounter++;}else if(p.tsc>=begin&&r.tsc<end){
    if(r.app&&r.app==p.app&&r.pid==p.pid&&r.pid==pid&&r.owner_req==r.owner_collect&&p.owner_req==p.owner_collect&&r.owner_req==p.owner_req){auto&c=cores[r.core];c.valid++;c.misses+=r.misses-p.misses;c.ticks+=r.tsc-p.tsc;bins[bin(r.tsc)].misses+=r.misses-p.misses;stats["llc_poll_interval_us"].add(r.tsc-p.tsc);}else badowner++;
  }}prev[r.core]=r;
 }
 void queue(const Queue&r){
  if(!in(r.tsc))return;
  pids["queue"][r.pid]++;
  if(r.pid!=pid)return;
  bool valid=!(r.flags&8);
  counts["invalid_runnable_queue"]+=!valid;
  uint64_t v[]={r.runnable,r.rx,r.timer,r.directpath,r.storage};
  const char*names[]={"runnable_us","rx_us","timer_us","directpath_us","storage_us"};
  for(int j=0;j<5;j++)if(j||valid)stats[names[j]].add(v[j]);
  if(valid)stats[(r.flags&1)?"runnable_active_us":"runnable_parked_us"].add(r.runnable);
  if(valid&&r.runnable>outliers.back().runnable){
   outliers.back()=r;
   std::sort(outliers.begin(),outliers.end(),[](const Queue&a,const Queue&b){return a.runnable>b.runnable;});
  }
  auto&b=bins[bin(r.tsc)];b.qn++;b.activeq+=bool(r.flags&1);
  b.work+=bool(r.flags&2);b.standing+=bool(r.flags&4);
  if(valid)b.qmax=std::max(b.qmax,r.runnable/cpu);
  b.directmax=std::max(b.directmax,r.directpath/cpu);b.timermax=std::max(b.timermax,r.timer/cpu);
  if(r.flags&1){auto&c=cores[r.core];c.qn++;c.work+=bool(r.flags&2);c.standing+=bool(r.flags&4);if(valid)c.qmax=std::max(c.qmax,r.runnable/cpu);}
 }
 void task(const Task&r){if(!in(r.tsc))return;pids["task"][r.pid]++;if(r.pid!=pid)return;stats["task_elapsed_us"].add(r.task);stats["kthread_elapsed_us"].add(r.kthread);auto&b=bins[bin(r.tsc)];b.taskn++;b.usern+=r.rcu&1;b.taskmax=std::max(b.taskmax,double(r.task));}
 void delay(const Delay&r){
  if(!in(r.tsc))return;
  pids["delay"][r.pid]++;
  if(r.pid!=pid)return;
  stats["process_rx_us"].add(r.rx);stats["active_threads"].add(r.active);
  auto&b=bins[bin(r.tsc)];b.delayn++;b.active_sum+=r.active;
  counts["invalid_process_delay"]+=bool(r.flags&8);
  if(!(r.flags&8)&&std::isfinite(r.max)&&r.max>=0){
   stats["process_max_delay_ns"].add(std::llround(r.max*1000));
   b.delaymax=std::max(b.delaymax,r.max);b.delaysum+=r.max;b.delayvalid++;
  }
  if(!(r.flags&8)&&std::isfinite(r.avg)&&r.avg>=0)stats["process_avg_delay_ns"].add(std::llround(r.avg*1000));
  counts["process_has_work"]+=bool(r.flags&1);counts["process_parked_busy"]+=bool(r.flags&2);counts["process_standing"]+=bool(r.flags&4);
 }
 void write(){
  std::ofstream extremes(out+"/queue-extremes.csv");extremes<<std::setprecision(12)<<"second,tid,core,active,has_work,standing,runnable_us,directpath_us\n";for(auto&r:outliers)if(r.runnable)extremes<<(r.tsc-begin)/hz<<','<<r.tid<<','<<r.core<<','<<bool(r.flags&1)<<','<<bool(r.flags&2)<<','<<bool(r.flags&4)<<','<<r.runnable/cpu<<','<<r.directpath/cpu<<'\n';
  std::ofstream summary(out+"/distributions.csv");summary<<std::setprecision(12)<<"signal,count,nonzero_count,nonzero_percent,mean,p50,p99,p99_9,max,positive_p50,positive_p99\n";
  for(auto&[name,s]:stats){double div=(name=="active_threads"||name=="task_elapsed_us"||name=="kthread_elapsed_us")?1:name.find("_ns")!=std::string::npos?1000:cpu;std::string label=name;auto i=label.find("_ns");if(i!=std::string::npos)label.replace(i,3,"_us");s.csv(summary,label,div);}
  std::ofstream ts(out+"/timeseries.csv");ts<<std::setprecision(12)<<"second,estimated_bandwidth_GB_s,bandwidth_coverage_seconds,llc_misses_per_second,queue_samples,active_queue_percent,has_work_percent,standing_percent,runnable_max_us,directpath_max_us,timer_max_us,task_max_us,process_delay_mean_us,process_delay_max_us,active_threads_mean,user_context_percent,scheduler_events,pmc_failures\n";
  for(int i=0;i<seconds;i++){auto&b=bins[i];ts<<i<<','<<(b.bwtime?double(b.bytes/b.bwtime)/1e9:0)<<','<<double(b.bwtime)<<','<<double(b.misses)<<','<<b.qn<<','<<(b.qn?100.*b.activeq/b.qn:0)<<','<<(b.qn?100.*b.work/b.qn:0)<<','<<(b.qn?100.*b.standing/b.qn:0)<<','<<b.qmax<<','<<b.directmax<<','<<b.timermax<<','<<b.taskmax<<','<<(b.delayvalid?b.delaysum/b.delayvalid:NAN)<<','<<b.delaymax<<','<<(b.delayn?double(b.active_sum)/b.delayn:0)<<','<<(b.taskn?100.*b.usern/b.taskn:0)<<','<<b.events<<','<<b.failures<<'\n';}
  std::ofstream core(out+"/per-core.csv");core<<std::setprecision(12)<<"core,llc_records,valid_llc_intervals,observed_misses,covered_seconds,misses_per_covered_second,active_queue_records,has_work_percent,standing_percent,runnable_max_us\n";
  for(auto&[id,c]:cores)core<<id<<','<<c.llcn<<','<<c.valid<<','<<c.misses<<','<<c.ticks/hz<<','<<(c.ticks?c.misses*hz/c.ticks:0)<<','<<c.qn<<','<<(c.qn?100.*c.work/c.qn:0)<<','<<(c.qn?100.*c.standing/c.qn:0)<<','<<c.qmax<<'\n';
  std::ofstream meta(out+"/counters.json");meta<<std::setprecision(12)<<"{\"bandwidth_covered_seconds\":"<<bwcoverage<<",\"estimated_mean_bandwidth_GB_s\":"<<(bwcoverage?double(totalbytes/bwcoverage)/1e9:0)<<",\"llc_nonpositive_time\":"<<badtime<<",\"llc_counter_decreases\":"<<badcounter<<",\"llc_owner_intervals_excluded\":"<<badowner<<",\"counts\":{";bool first=true;for(auto&[k,v]:counts){if(!first)meta<<',';first=false;meta<<'"'<<k<<"\":"<<v;}meta<<"},\"scheduler_reasons\":{";first=true;for(auto&[k,v]:events){if(!first)meta<<',';first=false;meta<<'"'<<k<<"\":"<<v;}meta<<"},\"pmc_failure_reasons\":{";first=true;for(auto&[k,v]:failures){if(!first)meta<<',';first=false;meta<<'"'<<k<<"\":"<<v;}meta<<"},\"observed_pids\":{";first=true;for(auto&[k,ps]:pids){if(!first)meta<<',';first=false;meta<<'"'<<k<<"\":{";bool inner=true;for(auto&[id,n]:ps){if(!inner)meta<<',';inner=false;meta<<'"'<<id<<"\":"<<n;}meta<<'}';}meta<<"}}\n";
 }
};
int main(int argc,char**argv){try{
 if(argc==5&&std::string(argv[1])=="--snapshot-frequency"){
  std::string prefix=argv[2];Header h{};
  std::ifstream file(prefix+".queue.bin",std::ios::binary);
  if(!file.read((char*)&h,sizeof(h))||h.type!=5||!std::isfinite(h.cpu)||h.cpu<=0)throw std::runtime_error("Invalid queue header");
  Analyzer a;a.begin=std::stoull(argv[3]);a.seconds=std::stoi(argv[4]);a.cpu=h.cpu;a.hz=h.cpu*1e6;
  if(a.seconds<=0)throw std::runtime_error("Invalid measurement duration");
  a.end=a.begin+uint64_t(a.seconds*a.hz);
  struct Counts{uint64_t attempts=0,invalid=0,busy=0,changed=0;};std::map<uint32_t,Counts> counts;
  a.scan<Queue>(prefix,"queue",[&](auto&r){if(a.in(r.tsc)){auto&c=counts[r.pid];c.attempts++;c.invalid+=bool(r.flags&8);}});
  a.scan<SnapshotFailure>(prefix,"queue-snapshot-failure",[&](auto&r){if(a.in(r.tsc)){auto&c=counts[r.pid];if(r.reason==1)c.busy++;else if(r.reason==2)c.changed++;else throw std::runtime_error("Unknown snapshot failure reason");}});
  std::cout<<std::setprecision(12)<<'{';bool first=true;
  for(auto&[pid,c]:counts){if(!c.attempts||c.invalid!=c.busy+c.changed)throw std::runtime_error("Snapshot failure counts do not match queue records");if(!first)std::cout<<',';first=false;std::cout<<'"'<<pid<<"\":{\"covered_seconds\":"<<a.seconds<<",\"attempts\":"<<c.attempts<<",\"writer_busy\":"<<c.busy<<",\"changed\":"<<c.changed<<",\"rejection_percent\":"<<100.*c.invalid/c.attempts<<'}';}
  std::cout<<"}\n";return 0;
 }
 if(argc!=7)throw std::runtime_error("usage: analyzer PREFIX START_TSC SECONDS PID OUTPUT CPU_TICKS_PER_US");Analyzer a;a.begin=std::stoull(argv[2]);a.seconds=std::stoi(argv[3]);a.pid=std::stoul(argv[4]);a.out=argv[5];a.cpu=std::stod(argv[6]);a.hz=a.cpu*1e6;a.end=a.begin+uint64_t(a.seconds*a.hz);a.bins.resize(a.seconds);
 std::ifstream head(std::string(argv[1])+".bw.bin",std::ios::binary);Header h{};head.read((char*)&h,sizeof(h));a.mult=h.multiplier;
 a.scan<BW>(argv[1],"bw",[&](auto&r){a.bandwidth(r);});a.scan<LLC>(argv[1],"llc",[&](auto&r){a.llc(r);});a.scan<Failure>(argv[1],"pmc-failure",[&](auto&r){if(a.in(r.tsc)){a.failures[r.reason]++;a.bins[a.bin(r.tsc)].failures++;}});a.scan<Sched>(argv[1],"sched",[&](auto&r){if(a.in(r.tsc)){a.events[r.reason]++;a.bins[a.bin(r.tsc)].events++;}});a.scan<Queue>(argv[1],"queue",[&](auto&r){a.queue(r);});a.scan<Task>(argv[1],"task",[&](auto&r){a.task(r);});a.scan<Delay>(argv[1],"delay",[&](auto&r){a.delay(r);});a.write();return 0;
}catch(std::exception&e){std::cerr<<e.what()<<'\n';return 1;}}

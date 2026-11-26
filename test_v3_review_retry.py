import unittest
from scripts.refresh_review import refresh


class ReviewRetryTests(unittest.TestCase):
    def run_case(self, responses, budget=900):
        t=[0]; calls=[]; logs=[]
        def request(method):
            calls.append((t[0],method))
            return responses(method,len(calls))
        def sleep(seconds):t[0]+=seconds
        return dict(request=request,clock=lambda:t[0],sleep=sleep,budget=budget,emit=logs.append),calls

    def test_deferred_uses_backoff_and_never_succeeds(self):
        kw,calls=self.run_case(lambda *a:dict(ok=True,state='deferred',computed_at='old',building=False))
        with self.assertRaisesRegex(TimeoutError,'deferred'):refresh(**kw)
        posts=[t for t,m in calls if m=='POST']
        self.assertEqual(posts,[0,60,180,420,720])
        self.assertEqual(len(calls),30)

    def test_building_is_status_only_until_saved(self):
        kw,calls=self.run_case(lambda m,n:dict(ok=True,state='fresh' if n==5 else 'building',building=n!=5,computed_at='saved' if n==5 else None))
        self.assertEqual(refresh(**kw)['computed_at'],'saved')
        self.assertEqual([m for _,m in calls],['POST','GET','GET','GET','GET'])

    def test_fresh_requires_timestamp_and_not_building(self):
        kw,calls=self.run_case(lambda m,n:dict(ok=True,state='fresh',computed_at=None if n==1 else 'saved',building=n==2))
        self.assertEqual(refresh(**kw)['computed_at'],'saved')
        self.assertEqual(len(calls),3)

    def test_invalid_payload_never_succeeds(self):
        kw,calls=self.run_case(lambda *a:[],budget=60)
        with self.assertRaisesRegex(TimeoutError,'unavailable'):refresh(**kw)

if __name__=='__main__':unittest.main()
